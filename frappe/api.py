# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
import base64
import binascii
import json
from typing import Literal
from urllib.parse import urlencode, urlparse
from frappe.modules.utils import get_doctype_module, get_module_app, get_module_name

import frappe
import frappe.client
import frappe.handler
from frappe import _
from frappe.utils.data import sbool
from frappe.utils.response import build_response


def handle():
    """
    Handler for `/api` methods

    ### Examples:

    `/api/method/{methodname}` will call a whitelisted method

    `/api/resource/{doctype}` will query a table
            examples:
            - `?fields=["name", "owner"]`
            - `?filters=[["Task", "name", "like", "%005"]]`
            - `?limit_start=0`
            - `?limit_page_length=20`

    `/api/resource/{doctype}/{name}` will point to a resource
            `GET` will return doclist
            `POST` will insert
            `PUT` will update
            `DELETE` will delete

    `/api/resource/{doctype}/{name}?run_method={method}` will run a whitelisted controller method
    """

    parts = frappe.request.path[1:].split("/")
    call = doctype = name = None

    if len(parts) > 1:
        call = parts[1]

    if len(parts) > 2:
        doctype = parts[2]
        if call == 'resource' and doctype[0].islower():
            doctype = frappe.unscrub(doctype)

    if len(parts) > 3:
        name = parts[3]

    if len(parts) > 4:
        frappe.local.form_dict.name = name
        module = get_doctype_module(doctype)
        app = get_module_app(module)
        doctype = get_module_name(doctype, module, "", "." + parts[4].replace("-", "_"), app)
        call = 'method'

    return _RESTAPIHandler(call, doctype, name).get_response()


class _RESTAPIHandler:
    def __init__(self, call: Literal["method", "resource"], doctype: str | None, name: str | None):
        self.call = call
        self.doctype = doctype
        self.name = name

    def get_response(self):
        """Prepare and get response based on URL and form body.

        Note: most methods of this class directly operate on the response local.
        """
        match self.call:
            case "method":
                return self.handle_method()
            case "resource":
                self.handle_resource()
            case _:
                raise frappe.DoesNotExistError

        return build_response("json")

    def handle_method(self):
        frappe.local.form_dict.cmd = self.doctype
        return frappe.handler.handle()

    def handle_resource(self):
        if self.doctype and self.name:
            self.handle_document_resource()
        elif self.doctype:
            self.handle_doctype_resource()
        else:
            raise frappe.DoesNotExistError

    def handle_document_resource(self):
        if "run_method" in frappe.local.form_dict:
            self.execute_doc_method()
            return

        match frappe.local.request.method:
            case "GET":
                self.get_doc()
            case "PUT":
                self.update_doc()
            case "DELETE":
                self.delete_doc()
            case _:
                raise frappe.DoesNotExistError

    def handle_doctype_resource(self):
        match frappe.local.request.method:
            case "GET":
                self.get_doc_list()
            case "POST":
                self.create_doc()
            case _:
                raise frappe.DoesNotExistError

    def execute_doc_method(self):
        method = frappe.local.form_dict.pop("run_method")
        doc = frappe.get_doc(self.doctype, self.name)
        doc.is_whitelisted(method)

        if frappe.local.request.method == "GET":
            if not doc.has_permission("read"):
                frappe.throw(_("Not permitted"), frappe.PermissionError)
            frappe.local.response.update({"data": doc.run_method(method, **frappe.local.form_dict)})

        elif frappe.local.request.method == "POST":
            if not doc.has_permission("write"):
                frappe.throw(_("Not permitted"), frappe.PermissionError)

            frappe.local.response.update({"data": doc.run_method(method, **frappe.local.form_dict)})
            frappe.db.commit()
    def _fields(self):
        fields = []
        if frappe.local.form_dict.get("fields"):
            if '[' in frappe.local.form_dict["fields"]:
                fields = json.loads(frappe.local.form_dict["fields"])
            else:
                fields = [f.strip() for f in frappe.local.form_dict["fields"].split(",")]
        #rename id to name
        if 'id' in fields: fields.remove("id")
        fields.append("name")
        return fields

    def get_doc(self):

        doc = frappe.get_doc(self.doctype, self.name)
        if not doc.has_permission("read"):
            raise frappe.PermissionError
        doc.apply_fieldlevel_read_permissions()

        fields = self._fields()

        d = doc.as_dict(no_nulls=True, no_default_fields=True, convert_dates_to_str=True)

        if fields:
            d = frappe._dict({field: d.get(field) for field in fields})

        d.update({"id": doc.name})

        frappe.local.response.update({"data": d})

    def update_doc(self):
        data = get_request_form_data()

        doc = frappe.get_doc(self.doctype, self.name, for_update=True)

        if "flags" in data:
            del data["flags"]

        # Not checking permissions here because it's checked in doc.save
        doc.update(data)

        frappe.local.response.update({"data": doc.save().as_dict(no_default_fields=True,
            convert_dates_to_str=True).update({"id": doc.name})})

        # check for child table doctype
        if doc.get("parenttype"):
            frappe.get_doc(doc.parenttype, doc.parent).save()
        frappe.db.commit()

    def delete_doc(self):
        # Not checking permissions here because it's checked in delete_doc
        frappe.delete_doc(self.doctype, self.name, ignore_missing=False)
        frappe.local.response.http_status_code = 202
        frappe.local.response.message = "ok"
        frappe.db.commit()

    def get_doc_list(self):
        if frappe.local.form_dict.get("fields"):
            frappe.local.form_dict["fields"] = self._fields()

        # set limit of records for frappe.get_list
        frappe.local.form_dict.setdefault(
            "limit_page_length",
            frappe.local.form_dict.limit or frappe.local.form_dict.limit_page_length or 20,
        )

        # convert strings to native types - only as_dict and debug accept bool
        for param in ["as_dict", "debug"]:
            param_val = frappe.local.form_dict.get(param)
            if param_val is not None:
                frappe.local.form_dict[param] = sbool(param_val)

        try:
            # evaluate frappe.get_list
            if self.doctype == 'Customer':
                filter = ["TSP List", 'tsp', '=', frappe.session.user.replace("@example.com", "")]
                if 'filters' in frappe.local.form_dict:
                    frappe.local.form_dict['filters'].append(filter)
                else:
                    frappe.local.form_dict['filters'] = [filter]
        except Exception as e:
            print(e)

        data = frappe.call(frappe.client.get_list, self.doctype, **frappe.local.form_dict)
        for d in data:
            if 'name' in d:
                d['id'] = d['name']
                del d['name']

        # set frappe.get_list result to response
        frappe.local.response.update({"data": data})

    def create_doc(self):
        data = get_request_form_data()
        data.update({"doctype": self.doctype})

        # insert document from request data
        doc = frappe.get_doc(data)
        doc.set("__islocal", True)
        doc.run_method("check_doc_exists")
        doc.save()

        if not frappe.local.response.get("http_status_code"):
            frappe.local.response["http_status_code"] = 201

          # set response data
        frappe.local.response.update({"data": doc.as_dict(
            no_default_fields=True,
            convert_dates_to_str=True,
          ).update({"id": doc.name})})

        # commit for POST requests
        frappe.db.commit()


def get_request_form_data():
    if frappe.local.form_dict.data is None:
        data = frappe.safe_decode(frappe.local.request.get_data())
    else:
        data = frappe.local.form_dict.data

    try:
        return frappe.parse_json(data)
    except ValueError:
        return frappe.local.form_dict


def validate_auth():
    """
    Authenticate and sets user for the request.
    """
    authorization_header = frappe.get_request_header("Authorization", "").split(" ")

    if len(authorization_header) == 2:
        validate_oauth(authorization_header)
        validate_auth_via_api_keys(authorization_header)

    validate_auth_via_hooks()


def validate_oauth(authorization_header):
    """
    Authenticate request using OAuth and set session user

    Args:
            authorization_header (list of str): The 'Authorization' header containing the prefix and token
    """

    from frappe.integrations.oauth2 import get_oauth_server
    from frappe.oauth import get_url_delimiter

    form_dict = frappe.local.form_dict
    token = authorization_header[1]
    req = frappe.request
    parsed_url = urlparse(req.url)
    access_token = {"access_token": token}
    uri = (
        parsed_url.scheme + "://" + parsed_url.netloc + parsed_url.path + "?" + urlencode(access_token)
    )
    http_method = req.method
    headers = req.headers
    body = req.get_data()
    if req.content_type and "multipart/form-data" in req.content_type:
        body = None

    try:
        required_scopes = frappe.db.get_value("OAuth Bearer Token", token, "scopes").split(
            get_url_delimiter()
        )
        valid, oauthlib_request = get_oauth_server().verify_request(
            uri, http_method, body, headers, required_scopes
        )
        if valid:
            frappe.set_user(frappe.db.get_value("OAuth Bearer Token", token, "user"))
            frappe.local.form_dict = form_dict
    except AttributeError:
        pass


def validate_auth_via_api_keys(authorization_header):
    """
    Authenticate request using API keys and set session user

    Args:
            authorization_header (list of str): The 'Authorization' header containing the prefix and token
    """

    try:
        auth_type, auth_token = authorization_header
        authorization_source = frappe.get_request_header("Frappe-Authorization-Source")
        if auth_type.lower() == "basic":
            api_key, api_secret = frappe.safe_decode(base64.b64decode(auth_token)).split(":")
            validate_api_key_secret(api_key, api_secret, authorization_source)
        elif auth_type.lower() == "token":
            api_key, api_secret = auth_token.split(":")
            validate_api_key_secret(api_key, api_secret, authorization_source)
    except binascii.Error:
        frappe.throw(
            _("Failed to decode token, please provide a valid base64-encoded token."),
            frappe.InvalidAuthorizationToken,
        )
    except (AttributeError, TypeError, ValueError):
        pass


def validate_api_key_secret(api_key, api_secret, frappe_authorization_source=None):
    """frappe_authorization_source to provide api key and secret for a doctype apart from User"""
    doctype = frappe_authorization_source or "User"
    cached = frappe.cache.hget("api_key", api_key)
    form_dict = frappe.local.form_dict

    if cached:
        doc = cached['user']
        doc_secret = cached["api_secret"]
    else:
        doc = frappe.db.get_value(doctype=doctype, filters={"api_key": api_key}, fieldname=["name"])
        doc_secret = frappe.utils.password.get_decrypted_password(doctype, doc, fieldname="api_secret")
        frappe.cache.hset("api_key", api_key, {"user": doc, "api_secret": doc_secret})

    if api_secret == doc_secret:
        if doctype == "User":
            user = doc
        else:
            user = frappe.db.get_value(doctype, doc, "user")

        frappe.local.api_user = True

        if frappe.local.login_manager.user in ("", "Guest"):
            frappe.set_user(user)
        frappe.local.form_dict = form_dict


def validate_auth_via_hooks():
    for auth_hook in frappe.get_hooks("auth_hooks", []):
        frappe.get_attr(auth_hook)()
