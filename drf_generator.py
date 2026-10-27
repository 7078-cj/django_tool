"""
DRF Generator - paste Django models, get serializers, views, urls, signals,
WebSocket consumers, ready-to-read API payload docs, and React components.

Run:  python drf_generator.py
Needs only the Python standard library (tkinter). Django is NOT required,
because models are parsed with the `ast` module, not imported.
"""

import ast
import bisect
import json
import keyword
import os
import re
import sys
import textwrap
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from urllib.parse import quote

SAMPLE = '''from django.db import models


class Toda(models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name


class RegisteredToda(models.Model):
    toda_number = models.CharField(max_length=20, unique=True)
    driver_name = models.CharField(max_length=100)
    vehicle_plate = models.CharField(max_length=20)
    registration_date = models.DateTimeField()
    toda = models.ForeignKey('Toda', on_delete=models.PROTECT, related_name='drivers')

    def __str__(self):
        return f"{self.toda_number} - {self.driver_name}"
'''


# --------------------------------------------------------------------------
# Model parsing
# --------------------------------------------------------------------------
RELATION_KINDS = {
    "ForeignKey": "fk",
    "OneToOneField": "o2o",
    "ManyToManyField": "m2m",
}


def _name_of(node):
    """Trailing name of a Name/Attribute node (models.Model -> Model)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_abstract(cls):
    for item in cls.body:
        if isinstance(item, ast.ClassDef) and item.name == "Meta":
            for stmt in item.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and any(_name_of(t) == "abstract" for t in stmt.targets)
                    and isinstance(stmt.value, ast.Constant)
                    and stmt.value.value is True
                ):
                    return True
    return False


def _model_fields(cls):
    """Return (field_names, relations) where relations is [(name, kind)]."""
    fields, relations = [], []
    for item in cls.body:
        targets, value = [], None
        if isinstance(item, ast.Assign):
            targets, value = item.targets, item.value
        elif isinstance(item, ast.AnnAssign) and item.value is not None:
            targets, value = [item.target], item.value
        if not isinstance(value, ast.Call):
            continue
        func = _name_of(value.func)
        if func == "GenericForeignKey":
            continue
        if func.endswith("Field") or func.endswith("Key"):
            for t in targets:
                if isinstance(t, ast.Name):
                    fields.append(t.id)
                    if func in RELATION_KINDS:
                        relations.append((t.id, RELATION_KINDS[func]))
    return fields, relations


def _model_classes(tree):
    """Concrete (non-abstract) Django model classes defined at module level."""
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if not any(
            _name_of(b).endswith("Model")
            or _name_of(b) in ("AbstractUser", "AbstractBaseUser")
            for b in node.bases
        ):
            continue
        if _is_abstract(node):
            continue
        yield node


def parse_models(source):
    """Return [(class_name, [field_names], [(relation_name, kind)])]."""
    tree = ast.parse(textwrap.dedent(source))
    return [(n.name, *_model_fields(n)) for n in _model_classes(tree)]


# --------------------------------------------------------------------------
# Field details (used for the API payload docs)
# --------------------------------------------------------------------------
INT_TYPES = {"IntegerField", "BigIntegerField", "SmallIntegerField",
             "PositiveIntegerField", "PositiveSmallIntegerField",
             "PositiveBigIntegerField", "AutoField", "BigAutoField",
             "SmallAutoField"}
AUTO_TYPES = {"AutoField", "BigAutoField", "SmallAutoField"}
FILE_TYPES = {"FileField", "ImageField"}


def _class_constants(cls):
    """Literal class attributes and TextChoices / IntegerChoices of a model."""
    consts, enums = {}, {}
    for item in cls.body:
        if (isinstance(item, ast.Assign) and len(item.targets) == 1
                and isinstance(item.targets[0], ast.Name)):
            try:
                consts[item.targets[0].id] = ast.literal_eval(item.value)
            except Exception:
                pass
        elif isinstance(item, ast.ClassDef) and any(
                _name_of(b).endswith("Choices") for b in item.bases):
            values = []
            for st in item.body:
                if (isinstance(st, ast.Assign) and len(st.targets) == 1
                        and isinstance(st.targets[0], ast.Name)):
                    try:
                        v = ast.literal_eval(st.value)
                    except Exception:
                        continue
                    values.append(v[0] if isinstance(v, tuple) else v)
            enums[item.name] = values
    return consts, enums


def _resolve(node, consts, enums):
    """Best-effort literal value of a keyword argument (None if unknown)."""
    if isinstance(node, ast.Name) and node.id in consts:
        return consts[node.id]
    if (isinstance(node, ast.Attribute) and node.attr == "choices"
            and isinstance(node.value, ast.Name) and node.value.id in enums):
        return [(v, str(v)) for v in enums[node.value.id]]
    try:
        return ast.literal_eval(node)
    except Exception:
        return None


def _flat_choices(choices):
    """Flatten Django choices (grouped ones too) into a list of stored values."""
    values = []
    if not isinstance(choices, (list, tuple)):
        return values
    for c in choices:
        if isinstance(c, (list, tuple)) and len(c) == 2:
            if isinstance(c[1], (list, tuple)):
                values += _flat_choices(c[1])
            else:
                values.append(c[0])
    return values


def parse_field_details(source):
    """{ClassName: {"fields": [info], "pk": name, "pk_type": type}}.

    Each info is {"name", "type", "kwargs", "has", "target"}.
    """
    tree = ast.parse(textwrap.dedent(source))
    result = {}
    for cls in _model_classes(tree):
        consts, enums = _class_constants(cls)
        infos = []
        for item in cls.body:
            targets, value = [], None
            if isinstance(item, ast.Assign):
                targets, value = item.targets, item.value
            elif isinstance(item, ast.AnnAssign) and item.value is not None:
                targets, value = [item.target], item.value
            if not isinstance(value, ast.Call):
                continue
            func = _name_of(value.func)
            if func == "GenericForeignKey" or not (
                    func.endswith("Field") or func.endswith("Key")):
                continue
            kwargs = {k.arg: _resolve(k.value, consts, enums)
                      for k in value.keywords if k.arg}
            has = {k.arg for k in value.keywords if k.arg}

            target = ""
            if func in RELATION_KINDS:
                to_node = value.args[0] if value.args else next(
                    (k.value for k in value.keywords if k.arg == "to"), None)
                if isinstance(to_node, ast.Constant) and isinstance(to_node.value, str):
                    target = to_node.value.split(".")[-1]
                elif to_node is not None:
                    target = _name_of(to_node)
                if target == "AUTH_USER_MODEL":
                    target = "User"

            for t in targets:
                if isinstance(t, ast.Name):
                    infos.append({"name": t.id, "type": func, "kwargs": kwargs,
                                  "has": has, "target": target})
        pk, pk_type = "id", "AutoField"
        for i in infos:
            if i["kwargs"].get("primary_key") is True:
                pk, pk_type = i["name"], i["type"]
        result[cls.name] = {"fields": infos, "pk": pk, "pk_type": pk_type}
    return result


def pk_converter(details, name):
    """URL converter for the model's primary key (int, uuid or str)."""
    pk_type = ((details or {}).get(name) or {}).get("pk_type", "AutoField")
    if pk_type == "UUIDField":
        return "uuid"
    if pk_type in ("CharField", "SlugField"):
        return "str"
    return "int"


# --------------------------------------------------------------------------
# Filtering (django-filter, search, ordering)
# --------------------------------------------------------------------------
SEARCH_TYPES = {"CharField", "TextField", "SlugField", "EmailField", "URLField"}
NUM_TYPES = INT_TYPES | {"FloatField", "DecimalField"}
NO_ORDER_TYPES = {"ManyToManyField", "JSONField", "FileField", "ImageField",
                  "BinaryField"}
NO_FILTER_TYPES = {"JSONField", "FileField", "ImageField", "BinaryField",
                   "DurationField"}

LOOKUP_DESC = {
    "exact": "equals",
    "icontains": "contains (ignore case)",
    "istartswith": "starts with (ignore case)",
    "gte": "greater than or equal",
    "lte": "less than or equal",
    "in": "one of (comma separated)",
}


def filter_lookups(info):
    """Lookups that make sense for this field type (empty list = no filter)."""
    t = info["type"]
    if t in NO_FILTER_TYPES:
        return []
    if _flat_choices(info["kwargs"].get("choices")):
        return ["exact", "in"]
    if t == "TextField":
        return ["icontains"]
    if t in SEARCH_TYPES:
        return ["exact", "icontains", "istartswith"]
    if t in NUM_TYPES or t == "DateField":
        return ["exact", "gte", "lte"]
    if t in ("DateTimeField", "TimeField"):
        return ["gte", "lte"]
    if (t in RELATION_KINDS
            or t in ("BooleanField", "NullBooleanField", "UUIDField",
                     "GenericIPAddressField", "IPAddressField")):
        return ["exact"]
    return []


def model_filter_spec(name, details):
    """{"lookups": {field: [lookup]}, "search": [...], "ordering": [...],
    "infos": {field: info}} for one model."""
    d = (details or {}).get(name) or {"fields": [], "pk": "id",
                                       "pk_type": "AutoField"}
    infos, pk = d["fields"], d["pk"]
    implicit_id = pk == "id" and not any(i["name"] == "id" for i in infos)
    lookups, search, ordering = {}, [], []
    if implicit_id:
        lookups["id"] = ["exact", "in"]
        ordering.append("id")
    for i in infos:
        lks = filter_lookups(i)
        if lks:
            lookups[i["name"]] = lks
        if i["type"] in SEARCH_TYPES and not _flat_choices(i["kwargs"].get("choices")):
            search.append(i["name"])
        if i["type"] not in NO_ORDER_TYPES:
            ordering.append(i["name"])
    return {"lookups": lookups, "search": search, "ordering": ordering,
            "infos": {i["name"]: i for i in infos}}


def build_filters(models, details, models_module=".models"):
    """filters.py: one django-filter FilterSet per model."""
    blocks, used = [], []
    for name, _, _ in models:
        lookups = model_filter_spec(name, details)["lookups"]
        if not lookups:
            continue
        used.append(name)
        block = [f"class {name}Filter(django_filters.FilterSet):",
                 "    class Meta:",
                 f"        model = {name}",
                 "        fields = {"]
        for field, lks in lookups.items():
            listing = ", ".join(f"'{lk}'" for lk in lks)
            block.append(f"            '{field}': [{listing}],")
        block.append("        }")
        blocks.append("\n".join(block))
    if not used:
        return ""
    head = [
        '"""',
        "django-filter FilterSets, one per model.",
        "",
        "Query examples:",
        "    ?field=value",
        "    ?field__icontains=text",
        "    ?field__gte=1&field__lte=10",
        "    ?field__in=a,b",
        '"""',
        "",
        "import django_filters",
        "",
        f"from {models_module} import {', '.join(used)}",
        "", "",
    ]
    return "\n".join(head) + "\n" + "\n\n\n".join(blocks) + "\n"


FILTERING_SETUP = '''# Filtering setup - copy these parts into your project.
#   pip install django-filter

# ---- settings.py -------------------------------------------------------
INSTALLED_APPS = [
    # ... your existing apps ...
    'rest_framework',
    'django_filters',
]

# The generated views already set filter_backends on their list views.
# To enable the backends for every API view instead, use:
# REST_FRAMEWORK = {
#     'DEFAULT_FILTER_BACKENDS': [
#         'django_filters.rest_framework.DjangoFilterBackend',
#         'rest_framework.filters.SearchFilter',
#         'rest_framework.filters.OrderingFilter',
#     ],
# }

# Recommended with filtering: paginate the list endpoints.
# REST_FRAMEWORK = {
#     'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
#     'PAGE_SIZE': 20,
# }
'''


def query_value(info, lookup):
    """Example query-string value for a filter."""
    if info is None:  # implicit id
        return "1,2" if lookup == "in" else "1"
    if lookup == "in":
        choices = _flat_choices(info["kwargs"].get("choices"))
        return ",".join(str(c) for c in choices[:2])
    v = sample_value(info)
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, dict)):
        return "1"
    return str(v)


def build_query_params(spec, filtering, search, ordering):
    """Query parameters of the list endpoint, for the payload docs."""
    query = []
    if filtering:
        for field, lks in spec["lookups"].items():
            info = spec["infos"].get(field)
            for lk in lks:
                query.append({
                    "param": field if lk == "exact" else f"{field}__{lk}",
                    "desc": f"{field} {LOOKUP_DESC[lk]}",
                    "example": query_value(info, lk)})
    if search and spec["search"]:
        query.append({"param": "search",
                      "desc": "text search in " + ", ".join(spec["search"]),
                      "example": "Sample"})
    if ordering and spec["ordering"]:
        query.append({"param": "ordering",
                      "desc": "sort by " + ", ".join(spec["ordering"])
                              + " (prefix - for descending, comma for several)",
                      "example": "-" + spec["ordering"][0]})
    return query


def query_example(query):
    """A realistic combined query string built from the parameters."""
    filters_ = [q for q in query if q["param"] not in ("search", "ordering")]
    first = next((q for q in filters_ if not q["param"].startswith("id")),
                 filters_[0] if filters_ else None)
    pick = [first] if first else []
    pick += [q for q in query if q["param"] in ("search", "ordering")]
    return "?" + "&".join(f"{q['param']}={quote(q['example'], safe=',:')}"
                          for q in pick)


# --------------------------------------------------------------------------
# API payload docs (structured data, rendered by the GUI)
# --------------------------------------------------------------------------
TEXT_HINTS = (("email", "user@example.com"), ("phone", "+1234567890"),
              ("mobile", "+1234567890"), ("url", "https://example.com"),
              ("website", "https://example.com"), ("plate", "ABC 1234"))

PERMISSION_NOTES = {
    "IsAuthenticated": "every request needs an authenticated user.",
    "IsAdminUser": "every request needs a staff user.",
    "IsAuthenticatedOrReadOnly":
        "reading is public; creating, updating and deleting need an authenticated user.",
    "AllowAny": "no authentication needed.",
}


def is_read_only(info):
    kw = info["kwargs"]
    return (info["type"] in AUTO_TYPES or kw.get("auto_now") is True
            or kw.get("auto_now_add") is True or kw.get("editable") is False)


def is_required(info):
    if is_read_only(info):
        return False
    kw = info["kwargs"]
    return not (kw.get("blank") is True or kw.get("null") is True
                or "default" in info["has"])


def sample_value(info, response=False):
    t, kw, name = info["type"], info["kwargs"], info["name"]
    choices = _flat_choices(kw.get("choices"))
    if choices:
        return choices[0]
    if t in ("ForeignKey", "OneToOneField"):
        return 1
    if t == "ManyToManyField":
        return [1, 2]
    if t in INT_TYPES:
        return 1
    if t == "FloatField":
        return 1.5
    if t == "DecimalField":
        places = kw.get("decimal_places")
        places = places if isinstance(places, int) else 2
        return f"{10.5:.{places}f}"
    if t in ("BooleanField", "NullBooleanField"):
        d = kw.get("default")
        return d if isinstance(d, bool) else True
    if t == "DateField":
        return "2026-10-26"
    if t == "DateTimeField":
        return "2026-10-26T08:00:00Z"
    if t == "TimeField":
        return "08:00:00"
    if t == "DurationField":
        return "00:30:00"
    if t == "JSONField":
        return {"key": "value"}
    if t == "UUIDField":
        return "3fa85f64-5717-4562-b3fc-2c963f66afa6"
    if t == "EmailField":
        return "user@example.com"
    if t == "URLField":
        return "https://example.com"
    if t == "SlugField":
        return "sample-slug"
    if t in ("GenericIPAddressField", "IPAddressField"):
        return "192.168.0.1"
    if t in FILE_TYPES:
        return "http://localhost:8000/media/sample.jpg" if response else "<file>"
    text = next((v for k, v in TEXT_HINTS if k in name),
                "Sample " + name.replace("_", " "))
    ml = kw.get("max_length")
    return text[:ml] if isinstance(ml, int) else text


def type_label(info):
    t, target = info["type"], info["target"]
    if t == "ManyToManyField":
        return f"array of {target or 'related'} ids"
    if t in ("ForeignKey", "OneToOneField"):
        return f"id of {target or 'related object'}"
    if t in INT_TYPES:
        return "integer"
    return {
        "FloatField": "number", "DecimalField": "decimal (string)",
        "BooleanField": "boolean", "NullBooleanField": "boolean",
        "DateField": "date (YYYY-MM-DD)",
        "DateTimeField": "datetime (ISO 8601)",
        "TimeField": "time (HH:MM:SS)", "DurationField": "duration (HH:MM:SS)",
        "JSONField": "object or array", "FileField": "file", "ImageField": "file",
    }.get(t, "string")


def field_notes(info):
    kw, t = info["kwargs"], info["type"]
    notes = []
    if kw.get("primary_key") is True:
        notes.append("primary key")
    if isinstance(kw.get("max_length"), int):
        notes.append(f"max length {kw['max_length']}")
    if kw.get("unique") is True:
        notes.append("unique")
    choices = _flat_choices(kw.get("choices"))
    if choices:
        notes.append("one of: " + ", ".join(str(c) for c in choices))
    if "default" in info["has"]:
        d = kw.get("default")
        notes.append("default " + json.dumps(d) if isinstance(d, (str, int, float, bool))
                     else "has a default")
    if kw.get("null") is True:
        notes.append("nullable")
    if kw.get("blank") is True and t in ("CharField", "TextField", "SlugField",
                                         "EmailField", "URLField"):
        notes.append("may be blank")
    if kw.get("auto_now") is True or kw.get("auto_now_add") is True:
        notes.append("set by the server")
    return "; ".join(notes)


def _jd(obj):
    return json.dumps(obj, indent=2, ensure_ascii=False)


def build_payload_docs(models, details, permission="None", websocket=True,
                       filtering=False, search=False, ordering=False):
    """Structured API docs, one dict per model.

    {"name", "route", "fields": [...], "rest_notes": [...],
     "rest": [endpoint], "ws": None | {"url", "notes", "messages", "errors"}}
    """
    details = details or {}
    unauth = {"detail": "Authentication credentials were not provided."}
    forbid = {"detail": "You do not have permission to perform this action."}
    not_found = {"detail": "Not found."}

    def R(status, text, body=None):
        return {"status": status, "text": text, "body": body}

    def auth_responses(write):
        if permission == "IsAuthenticated":
            return [R(401, "Not authenticated", unauth)]
        if permission == "IsAdminUser":
            return [R(401, "Not authenticated", unauth),
                    R(403, "Not a staff user", forbid)]
        if permission == "IsAuthenticatedOrReadOnly" and write:
            return [R(401, "Not authenticated", unauth)]
        return []

    docs = []
    for name, _fields, _relations in models:
        d = details.get(name) or {"fields": [], "pk": "id", "pk_type": "AutoField"}
        infos, pk = d["fields"], d["pk"]
        implicit_id = pk == "id" and not any(i["name"] == "id" for i in infos)
        route = plural(snake(name)).replace("_", "-")

        resp = {"id": 1} if implicit_id else {}
        for i in infos:
            resp[i["name"]] = sample_value(i, response=True)
        body = {i["name"]: sample_value(i) for i in infos if not is_read_only(i)}
        patch = dict(list(body.items())[:1])
        pk_val = resp.get(pk, 1)
        list_path = f"{{base}}/{route}/"
        item_path = f"{{base}}/{route}/{pk_val}/"

        required = [i["name"] for i in infos if is_required(i)]
        err_key = required[0] if required else next(iter(body), None)
        err = ({err_key: ["This field is required." if required
                          else "Enter a valid value."]} if err_key else None)
        patch_err = ({next(iter(patch)): ["Enter a valid value."]}
                     if patch else None)

        files = [i["name"] for i in infos if i["type"] in FILE_TYPES]
        ctype = "multipart/form-data" if files else "application/json"
        file_note = (f"File field(s) {', '.join(files)}: send as multipart/form-data "
                     "(the JSON below shows the other fields)." if files else None)

        fields = []
        if implicit_id:
            fields.append({"name": "id", "type": "integer",
                           "required": "read-only", "notes": "primary key"})
        for i in infos:
            fields.append({
                "name": i["name"], "type": type_label(i),
                "required": ("read-only" if is_read_only(i)
                             else "yes" if is_required(i) else "no"),
                "notes": field_notes(i)})

        def ep(title, method, path, request=None, responses=(), write=False,
               note=None):
            return {"title": title, "method": method, "path": path,
                    "content_type": ctype if request is not None else None,
                    "request": request, "note": note if request is not None else None,
                    "responses": list(responses) + auth_responses(write)}

        create_resp = [R(201, "Created, returns the new object", resp)]
        if err:
            create_resp.append(R(400, "Validation error", err))
        put_resp = [R(200, "Updated, returns the object", resp)]
        if err:
            put_resp.append(R(400, "Validation error", err))
        put_resp.append(R(404, "Unknown id", not_found))
        patch_resp = [R(200, "Updated, returns the object", resp)]
        if patch_err:
            patch_resp.append(R(400, "Validation error", patch_err))
        patch_resp.append(R(404, "Unknown id", not_found))

        rest = [
            ep("List", "GET", list_path, responses=[R(200, "Array of objects", [resp])]),
            ep("Create", "POST", list_path, body, create_resp, True, file_note),
            ep("Retrieve", "GET", item_path,
               responses=[R(200, "The object", resp), R(404, "Unknown id", not_found)]),
            ep("Update (full)", "PUT", item_path, body, put_resp, True, file_note),
            ep("Update (partial)", "PATCH", item_path, patch, patch_resp, True,
               file_note),
            ep("Delete", "DELETE", item_path,
               responses=[R(204, "Deleted, no body"),
                          R(404, "Unknown id", not_found)], write=True),
        ]

        rest_notes = [
            "Replace {base} with the prefix where the app's urls.py is included "
            "(for example /api).",
            "Optional fields can be left out of requests. Responses always "
            "contain every field.",
            "The list endpoint returns a plain array. With DRF pagination it is "
            "wrapped in count, next, previous and results.",
        ]
        if permission in PERMISSION_NOTES:
            rest_notes.append(f"Permission {permission}: {PERMISSION_NOTES[permission]}")

        query = build_query_params(model_filter_spec(name, details),
                                   filtering, search, ordering)
        if query:
            rest[0]["query"] = query
            rest[0]["query_example"] = query_example(query)
            rest_notes.append("Filters, search and ordering are query-string "
                              "parameters of the list endpoint and can be combined.")

        ws = None
        if websocket:
            ws_notes = ["Replace HOST with your server (for example localhost:8000).",
                        "Every message is JSON. request_id is optional; when you "
                        "send one it is echoed back so you can match replies to "
                        "requests."]
            if permission == "IsAuthenticated":
                ws_notes.append("The connection is closed with code 4401 when the "
                                "user is not authenticated.")
            elif permission == "IsAdminUser":
                ws_notes.append("The connection is closed with code 4403 when the "
                                "user is not staff.")
            elif permission == "IsAuthenticatedOrReadOnly":
                ws_notes.append("Anyone can connect and read. create, update and "
                                "delete need an authenticated user.")
            if files:
                ws_notes.append(f"File fields ({', '.join(files)}) cannot be sent "
                                "over WebSocket (JSON only). Use REST for uploads.")

            messages = [
                {"title": "list", "summary": "Get every object.",
                 "send": {"action": "list", "request_id": "req-1"},
                 "receive": {"action": "list", "request_id": "req-1", "data": [resp]},
                 "push": None},
                {"title": "create", "summary": "Create an object. Other clients are notified.",
                 "send": {"action": "create", "request_id": "req-2", "data": body},
                 "receive": {"action": "create", "request_id": "req-2", "data": resp},
                 "push": {"action": "created", "data": resp}},
                {"title": "retrieve", "summary": "Get one object by id.",
                 "send": {"action": "retrieve", "id": pk_val, "request_id": "req-3"},
                 "receive": {"action": "retrieve", "request_id": "req-3", "data": resp},
                 "push": None},
                {"title": "update", "summary": "Partial update. Other clients are notified.",
                 "send": {"action": "update", "id": pk_val, "request_id": "req-4",
                          "data": patch},
                 "receive": {"action": "update", "request_id": "req-4", "data": resp},
                 "push": {"action": "updated", "data": resp}},
                {"title": "delete", "summary": "Delete an object. Other clients are notified.",
                 "send": {"action": "delete", "id": pk_val, "request_id": "req-5"},
                 "receive": {"action": "delete", "request_id": "req-5", "id": pk_val},
                 "push": {"action": "deleted", "data": {"id": pk_val}}},
            ]
            errors = []
            if err:
                errors.append({"title": "Validation error",
                               "body": {"action": "error", "request_id": "req-2",
                                        "errors": err}})
            errors += [
                {"title": "Unknown id",
                 "body": {"action": "error", "request_id": "req-3",
                          "errors": "Not found"}},
                {"title": "Missing id",
                 "body": {"action": "error", "request_id": "req-3",
                          "errors": "id is required"}},
                {"title": "Unknown action",
                 "body": {"action": "error", "request_id": None,
                          "errors": "Unknown action: foo"}},
            ]
            if permission == "IsAuthenticatedOrReadOnly":
                errors.append({"title": "Not authenticated (write actions)",
                               "body": {"action": "error", "request_id": "req-2",
                                        "errors": "Authentication required"}})
            ws = {"url": f"ws://HOST/ws/{route}/", "notes": ws_notes,
                  "messages": messages, "errors": errors}

        docs.append({"name": name, "route": route, "fields": fields,
                     "rest_notes": rest_notes, "rest": rest, "ws": ws})
    return docs


# --------------------------------------------------------------------------
# Code generation
# --------------------------------------------------------------------------
def snake(name):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def plural(word):
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return word + "es"
    if word.endswith("y") and len(word) > 1 and word[-2] not in "aeiou":
        return word[:-1] + "ies"
    return word + "s"


def build_queryset(name, relations, optimize):
    """Queryset expression; uses select_related / prefetch_related to avoid N+1."""
    plain = f"{name}.objects.all()"
    if not optimize:
        return plain
    select = [f for f, k in relations if k in ("fk", "o2o")]
    prefetch = [f for f, k in relations if k == "m2m"]
    if not select and not prefetch:
        return plain

    chain = [f"{name}.objects"]
    if select:
        chain.append(".select_related(" + ", ".join(f"'{f}'" for f in select) + ")")
    if prefetch:
        chain.append(".prefetch_related(" + ", ".join(f"'{f}'" for f in prefetch) + ")")

    one_line = "".join(chain)
    if len("    queryset = ") + len(one_line) <= 79:
        return one_line
    return "(\n        " + "\n        ".join(chain) + "\n    )"


SIGNAL_ORDER = ("pre_save", "post_save", "pre_delete", "post_delete")


def build_signals(models, signals, models_module=".models"):
    """signals.py with one @receiver per model per selected signal."""
    chosen = [s for s in SIGNAL_ORDER if s in signals]
    if not chosen:
        return ""
    lines = [
        '"""',
        "Signal receivers.",
        "",
        "Activate them by importing this module when the app loads, in apps.py:",
        "",
        "    class MyAppConfig(AppConfig):",
        "        def ready(self):",
        "            from . import signals  # noqa: F401",
        '"""',
        "",
        f"from django.db.models.signals import {', '.join(sorted(chosen))}",
        "from django.dispatch import receiver",
        "",
        f"from {models_module} import {', '.join(m[0] for m in models)}",
        "", "",
    ]
    for name, _, _ in models:
        fn = snake(name)
        for sig in chosen:
            if sig == "post_save":
                args = "sender, instance, created, **kwargs"
                body = ["    if created:",
                        f"        pass  # new {name} created",
                        "    else:",
                        f"        pass  # existing {name} updated"]
            else:
                args = "sender, instance, **kwargs"
                body = ["    pass"]
            lines += [f"@receiver({sig}, sender={name})",
                      f"def {fn}_{sig}({args}):"] + body + ["", ""]
    return "\n".join(lines).rstrip() + "\n"


SNIPPET_ONLY = {"channels_setup.py", "filtering_setup.py"}  # shown in a tab, not saved to the app folder

CONSUMER_TEMPLATE = '''class __NAME__Consumer(AsyncJsonWebsocketConsumer):
    group_name = '__GROUP__'
    queryset = __QUERYSET__
    serializer_class = __NAME__Serializer

    async def connect(self):
__CONNECT_CHECK__        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        action = content.get('action')
        handler = {
            'list': self.handle_list,
            'create': self.handle_create,
            'retrieve': self.handle_retrieve,
            'update': self.handle_update,
            'delete': self.handle_delete,
        }.get(action)
        if handler is None:
            await self.reply(content, 'error', errors=f'Unknown action: {action}')
            return
__WRITE_CHECK__        if action in ('retrieve', 'update', 'delete') and content.get('id') is None:
            await self.reply(content, 'error', errors='id is required')
            return
        try:
            await handler(content)
        except __NAME__.DoesNotExist:
            await self.reply(content, 'error', errors='Not found')
        except (ValueError, TypeError):
            await self.reply(content, 'error', errors='Invalid request')

    # ---- helpers ---------------------------------------------------------
    async def reply(self, content, action, **payload):
        await self.send_json(
            {'action': action, 'request_id': content.get('request_id'), **payload}
        )

    async def broadcast(self, event, data):
        await self.channel_layer.group_send(
            self.group_name,
            {'type': 'model.event', 'event': event, 'data': data,
             'sender': self.channel_name},
        )

    async def model_event(self, message):
        if message['sender'] == self.channel_name:
            return  # the sender already got a direct reply
        await self.send_json({'action': message['event'], 'data': message['data']})

    # ---- actions ---------------------------------------------------------
    async def handle_list(self, content):
        data = await self.db_list()
        await self.reply(content, 'list', data=data)

    async def handle_create(self, content):
        data, errors = await self.db_create(content.get('data') or {})
        if errors:
            await self.reply(content, 'error', errors=errors)
            return
        await self.reply(content, 'create', data=data)
        await self.broadcast('created', data)

    async def handle_retrieve(self, content):
        data = await self.db_retrieve(content['id'])
        await self.reply(content, 'retrieve', data=data)

    async def handle_update(self, content):
        data, errors = await self.db_update(content['id'], content.get('data') or {})
        if errors:
            await self.reply(content, 'error', errors=errors)
            return
        await self.reply(content, 'update', data=data)
        await self.broadcast('updated', data)

    async def handle_delete(self, content):
        await self.db_delete(content['id'])
        await self.reply(content, 'delete', id=content['id'])
        await self.broadcast('deleted', {'id': content['id']})

    # ---- database (sync code, run in a thread) ---------------------------
    @database_sync_to_async
    def db_list(self):
        return self.serializer_class(self.queryset.all(), many=True).data

    @database_sync_to_async
    def db_create(self, data):
        serializer = self.serializer_class(data=data)
        if not serializer.is_valid():
            return None, serializer.errors
        serializer.save()
        return serializer.data, None

    @database_sync_to_async
    def db_retrieve(self, pk):
        return self.serializer_class(self.queryset.get(pk=pk)).data

    @database_sync_to_async
    def db_update(self, pk, data):
        instance = self.queryset.get(pk=pk)
        serializer = self.serializer_class(instance, data=data, partial=True)
        if not serializer.is_valid():
            return None, serializer.errors
        serializer.save()
        return serializer.data, None

    @database_sync_to_async
    def db_delete(self, pk):
        self.queryset.get(pk=pk).delete()
'''

CHANNELS_SETUP = '''# Django Channels setup - copy these parts into your project.
#   pip install channels daphne
#   pip install channels-redis      (production only)
# Replace "myproject" and "myapp" with your project and app names.

# ---- settings.py -------------------------------------------------------
INSTALLED_APPS = [
    'daphne',  # must be first, so runserver serves ASGI
    # ... your existing apps ...
    'channels',
]

ASGI_APPLICATION = 'myproject.asgi.application'

CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels.layers.InMemoryChannelLayer',  # development only
    },
}

# Production: use Redis so every worker shares one channel layer.
# CHANNEL_LAYERS = {
#     'default': {
#         'BACKEND': 'channels_redis.core.RedisChannelLayer',
#         'CONFIG': {'hosts': [('127.0.0.1', 6379)]},
#     },
# }


# ---- myproject/asgi.py -------------------------------------------------
import os

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'myproject.settings')
django_asgi_app = get_asgi_application()

from myapp import routing  # noqa: E402  (must come after get_asgi_application)

application = ProtocolTypeRouter({
    'http': django_asgi_app,
    'websocket': AuthMiddlewareStack(
        URLRouter(routing.websocket_urlpatterns)
    ),
})
'''


def build_consumers(models, permission="None", models_module=".models",
                    optimize=True):
    """consumers.py: one AsyncJsonWebsocketConsumer with CRUD per model."""
    names = [m[0] for m in models]
    out = [
        '"""',
        "WebSocket CRUD consumers (Django Channels).",
        "",
        "Client messages (JSON):",
        '    {"action": "list"}',
        '    {"action": "create", "data": {...}}',
        '    {"action": "retrieve", "id": 1}',
        '    {"action": "update", "id": 1, "data": {...}}',
        '    {"action": "delete", "id": 1}',
        'Add "request_id" to any message and it is echoed back in the reply.',
        'Other connected clients receive "created" / "updated" / "deleted" pushes.',
        '"""',
        "",
        "from channels.db import database_sync_to_async",
        "from channels.generic.websocket import AsyncJsonWebsocketConsumer",
        "",
        f"from {models_module} import {', '.join(names)}",
        f"from .serializers import {', '.join(n + 'Serializer' for n in names)}",
        "", "",
    ]

    connect_check = ""
    write_check = ""
    if permission == "IsAuthenticated":
        connect_check = ("        user = self.scope.get('user')\n"
                         "        if not (user and user.is_authenticated):\n"
                         "            await self.close(code=4401)\n"
                         "            return\n")
    elif permission == "IsAdminUser":
        connect_check = ("        user = self.scope.get('user')\n"
                         "        if not (user and user.is_staff):\n"
                         "            await self.close(code=4403)\n"
                         "            return\n")
    elif permission == "IsAuthenticatedOrReadOnly":
        write_check = ("        is_write = action in ('create', 'update', 'delete')\n"
                       "        user = self.scope.get('user')\n"
                       "        if is_write and not (user and user.is_authenticated):\n"
                       "            await self.reply(content, 'error',\n"
                       "                             errors='Authentication required')\n"
                       "            return\n")

    blocks = []
    for name, _, relations in models:
        block = (CONSUMER_TEMPLATE
                 .replace("__CONNECT_CHECK__", connect_check)
                 .replace("__WRITE_CHECK__", write_check)
                 .replace("__QUERYSET__", build_queryset(name, relations, optimize))
                 .replace("__GROUP__", plural(snake(name)))
                 .replace("__NAME__", name))
        blocks.append(block.rstrip())
    return "\n".join(out) + "\n" + "\n\n\n".join(blocks) + "\n"


def build_routing(models):
    """routing.py: websocket_urlpatterns, same URL naming as urls.py."""
    lines = ["from django.urls import path", "", "from .consumers import ("]
    lines += [f"    {name}Consumer," for name, _, _ in models]
    lines += [")", "", "websocket_urlpatterns = ["]
    for name, _, _ in models:
        route = plural(snake(name)).replace("_", "-")
        lines.append(f"    path('ws/{route}/', {name}Consumer.as_asgi()),")
    lines.append("]")
    return "\n".join(lines) + "\n"


def build_tests(models, details, permission="None", models_module=".models"):
    """tests.py: APIClient CRUD, validation, not-found, and permission tests."""
    names = [name for name, _, _ in models]
    model_payloads = {}
    model_relations = {}
    for name in names:
        detail = (details or {}).get(
            name, {"fields": [], "pk": "id", "pk_type": "AutoField"})
        infos = detail["fields"]
        model_payloads[name] = {
            info["name"]: sample_value(info)
            for info in infos if not is_read_only(info)
        }
        model_relations[name] = [
            (info["name"], info["target"], info["type"])
            for info in infos
            if info["type"] in ("ForeignKey", "OneToOneField",
                                "ManyToManyField")
            and info["target"] in names
        ]
    lines = [
        '"""Generated REST API tests. Adjust sample values to match your fixtures."""',
        "",
        "import uuid",
        "from types import SimpleNamespace",
        "",
        "from django.urls import reverse",
        "from rest_framework.test import APITestCase",
        "",
        f"from {models_module} import {', '.join(names)}",
        "", "",
    ]
    if any(model_relations.values()):
        lines += [
            f"_TEST_PAYLOADS = {model_payloads!r}",
            "",
            "def _create_related_instance(model_class, creating=()):",
            "    if model_class in creating:",
            "        raise ValueError(",
            "            'Circular model relation; provide a project-specific fixture.')",
            "    payload = dict(_TEST_PAYLOADS.get(model_class.__name__, {}))",
            "    many_to_many = model_class._meta.many_to_many",
            "    for field in many_to_many:",
            "        payload.pop(field.name, None)",
            "    for field in model_class._meta.concrete_fields:",
            "        if not field.is_relation:",
            "            continue",
            "        related_model = field.remote_field.model",
            "        if related_model.__name__ in _TEST_PAYLOADS:",
            "            related = _create_related_instance(",
            "                related_model, creating + (model_class,))",
            "            payload[field.name] = related.pk",
            "    instance = model_class.objects.create(**payload)",
            "    for field in many_to_many:",
            "        related_model = field.remote_field.model",
            "        if related_model.__name__ in _TEST_PAYLOADS:",
            "            related = _create_related_instance(",
            "                related_model, creating + (model_class,))",
            "            getattr(instance, field.name).add(related)",
            "    return instance",
            "", "",
        ]

    for name, _, _ in models:
        detail = (details or {}).get(
            name, {"fields": [], "pk": "id", "pk_type": "AutoField"})
        infos = detail["fields"]
        pk = detail["pk"]
        payload = model_payloads[name]
        route_name = snake(name)
        class_name = f"{name}APITests"
        lines += [
            f"class {class_name}(APITestCase):",
            f"    model = {name}",
            f"    list_url_name = '{route_name}-list'",
            f"    detail_url_name = '{route_name}-detail'",
            f"    pk_field = '{pk}'",
        ]
        if any(info["type"] in ("ForeignKey", "OneToOneField",
                                "ManyToManyField")
               and info["target"] not in names for info in infos):
            lines.append(
                "    # Set up related records for models not included in this input.")
        lines += [
            f"    valid_payload = {payload!r}",
            "",
        ]
        if model_relations[name]:
            lines += [
                "    def setUp(self):",
                "        super().setUp()",
                "        self.valid_payload = dict(self.valid_payload)",
            ]
            for field_name, target, field_type in model_relations[name]:
                lines += [
                    f"        related_{field_name} = _create_related_instance({target})",
                    f"        self.valid_payload['{field_name}'] = "
                    f"[related_{field_name}.pk]"
                    if field_type == "ManyToManyField"
                    else f"        self.valid_payload['{field_name}'] = "
                    f"related_{field_name}.pk",
                ]
            lines.append("")
        lines += [
            "    def authenticate(self, is_staff=True):",
            "        self.client.force_authenticate(user=SimpleNamespace(",
            "            is_authenticated=True, is_staff=is_staff))",
            "",
            "    def list_url(self):",
            "        return reverse(self.list_url_name)",
            "",
            "    def detail_url(self, pk):",
            "        return reverse(self.detail_url_name, kwargs={'pk': pk})",
            "",
            "    def missing_pk(self):",
        ]
        if detail["pk_type"] == "UUIDField":
            lines += [
                "        candidate = uuid.UUID('00000000-0000-0000-0000-000000000001')",
                "        while self.model.objects.filter(pk=candidate).exists():",
                "            candidate = uuid.uuid4()",
                "        return candidate",
            ]
        elif detail["pk_type"] in ("CharField", "SlugField"):
            lines += [
                "        candidate = '__missing_test_record__'",
                "        while self.model.objects.filter(pk=candidate).exists():",
                "            candidate += '_'",
                "        return candidate",
            ]
        else:
            lines += [
                "        last_pk = self.model.objects.order_by('-pk').values_list(",
                "            'pk', flat=True).first()",
                "        return (last_pk or 0) + 1",
            ]
        lines += [
            "",
            "    def test_crud_happy_path(self):",
            "        self.authenticate()",
            "        created = self.client.post(self.list_url(), self.valid_payload,",
            "                                   format='json')",
            "        self.assertEqual(created.status_code, 201, created.data)",
            "        self.assertIn(self.pk_field, created.data)",
            "        pk = created.data[self.pk_field]",
            "",
            "        listed = self.client.get(self.list_url())",
            "        self.assertEqual(listed.status_code, 200)",
            "        retrieved = self.client.get(self.detail_url(pk))",
            "        self.assertEqual(retrieved.status_code, 200)",
            "",
            "        writable = next(iter(self.valid_payload), None)",
            "        if writable:",
            "            updated = self.client.patch(",
            "                self.detail_url(pk), {writable: self.valid_payload[writable]},",
            "                format='json')",
            "            self.assertEqual(updated.status_code, 200, updated.data)",
            "",
            "        deleted = self.client.delete(self.detail_url(pk))",
            "        self.assertEqual(deleted.status_code, 204)",
            "        missing = self.client.get(self.detail_url(pk))",
            "        self.assertEqual(missing.status_code, 404)",
            "",
            "    def test_invalid_payload_returns_400(self):",
            "        self.authenticate()",
            "        response = self.client.post(self.list_url(), [], format='json')",
            "        self.assertEqual(response.status_code, 400)",
            "",
            "    def test_unknown_record_returns_404(self):",
            "        self.authenticate()",
            "        response = self.client.get(self.detail_url(self.missing_pk()))",
            "        self.assertEqual(response.status_code, 404)",
        ]
        if permission == "IsAuthenticated":
            lines += [
                "",
                "    def test_authentication_is_required(self):",
                "        response = self.client.get(self.list_url())",
                "        self.assertIn(response.status_code, (401, 403))",
            ]
        elif permission == "IsAdminUser":
            lines += [
                "",
                "    def test_staff_access_is_required(self):",
                "        response = self.client.get(self.list_url())",
                "        self.assertIn(response.status_code, (401, 403))",
                "        self.authenticate(is_staff=False)",
                "        response = self.client.get(self.list_url())",
                "        self.assertEqual(response.status_code, 403)",
            ]
        elif permission == "IsAuthenticatedOrReadOnly":
            lines += [
                "",
                "    def test_unauthenticated_write_is_denied(self):",
                "        response = self.client.post(",
                "            self.list_url(), self.valid_payload, format='json')",
                "        self.assertIn(response.status_code, (401, 403))",
                "        self.assertEqual(self.client.get(self.list_url()).status_code, 200)",
            ]
        lines += ["", ""]
    return "\n".join(lines).rstrip() + "\n"


def generate(models, explicit_fields=False, permission="None", with_urls=True,
             models_module=".models", optimize=True, signals=(), websocket=False,
             details=None, filtering=False, search=False, ordering=False):
    names = [m[0] for m in models]

    # serializers.py
    ser = ["from rest_framework import serializers", "",
           f"from {models_module} import {', '.join(names)}", "", ""]
    for name, fields, _ in models:
        ser += [f"class {name}Serializer(serializers.ModelSerializer):",
                "    class Meta:",
                f"        model = {name}"]
        if explicit_fields and fields:
            pk = (details or {}).get(name, {}).get("pk", "id")
            listing_fields = fields if pk != "id" else ["id"] + fields
            listing = ", ".join(f"'{f}'" for f in listing_fields)
            ser.append(f"        fields = [{listing}]")
        else:
            ser.append("        fields = '__all__'")
        ser += ["", ""]
    serializers_py = "\n".join(ser).rstrip() + "\n"

    # views.py
    specs = {n: model_filter_spec(n, details) for n in names}
    use_filterset = {n for n in names if filtering and specs[n]["lookups"]}
    use_search = {n for n in names if search and specs[n]["search"]}
    use_order = {n for n in names if ordering and specs[n]["ordering"]}

    imports = []
    if use_filterset:
        imports.append("from django_filters.rest_framework import DjangoFilterBackend")
    imports.append("from rest_framework import filters, generics"
                   if (use_search or use_order)
                   else "from rest_framework import generics")
    if permission != "None":
        imports.append(f"from rest_framework.permissions import {permission}")
    local = [f"from {models_module} import {', '.join(names)}"]
    if use_filterset:
        local.append("from .filters import "
                     + ", ".join(f"{n}Filter" for n in names if n in use_filterset))
    local.append(f"from .serializers import {', '.join(n + 'Serializer' for n in names)}")
    view = imports + [""] + local + ["", ""]
    for name, _, relations in models:
        queryset = build_queryset(name, relations, optimize)
        for suffix, base in (("ListCreateView", "ListCreateAPIView"),
                             ("DetailView", "RetrieveUpdateDestroyAPIView")):
            view += [f"class {name}{suffix}(generics.{base}):",
                     f"    queryset = {queryset}",
                     f"    serializer_class = {name}Serializer"]
            if permission != "None":
                view.append(f"    permission_classes = [{permission}]")
            if suffix == "ListCreateView":
                backends = []
                if name in use_filterset:
                    backends.append("DjangoFilterBackend")
                if name in use_search:
                    backends.append("filters.SearchFilter")
                if name in use_order:
                    backends.append("filters.OrderingFilter")
                if backends:
                    view.append(f"    filter_backends = [{', '.join(backends)}]")
                if name in use_filterset:
                    view.append(f"    filterset_class = {name}Filter")
                if name in use_search:
                    view.append(f"    search_fields = {specs[name]['search']!r}")
                if name in use_order:
                    view.append(f"    ordering_fields = {specs[name]['ordering']!r}")
                    view.append("    ordering = ['pk']")
            view += ["", ""]
    views_py = "\n".join(view).rstrip() + "\n"

    filters_py = (build_filters(models, details, models_module)
                  if filtering else "")
    filtering_setup = FILTERING_SETUP if filters_py else ""

    # urls.py
    urls_py = ""
    if with_urls:
        u = ["from django.urls import path", "", "from .views import ("]
        for name, _, _ in models:
            u += [f"    {name}ListCreateView,", f"    {name}DetailView,"]
        u += [")", "", "urlpatterns = ["]
        for name, _, _ in models:
            route = plural(snake(name)).replace("_", "-")
            conv = pk_converter(details, name)
            u += [
                f"    path('{route}/', {name}ListCreateView.as_view(), "
                f"name='{snake(name)}-list'),",
                f"    path('{route}/<{conv}:pk>/', {name}DetailView.as_view(), "
                f"name='{snake(name)}-detail'),",
            ]
        u.append("]")
        urls_py = "\n".join(u) + "\n"

    signals_py = build_signals(models, signals, models_module)

    consumers_py = routing_py = setup_py = ""
    if websocket:
        consumers_py = build_consumers(models, permission, models_module, optimize)
        routing_py = build_routing(models)
        setup_py = CHANNELS_SETUP

    tests_py = build_tests(models, details, permission, models_module)
    return {"serializers.py": serializers_py, "views.py": views_py,
            "urls.py": urls_py, "filters.py": filters_py,
            "signals.py": signals_py,
            "consumers.py": consumers_py, "routing.py": routing_py,
            "channels_setup.py": setup_py,
            "filtering_setup.py": filtering_setup, "tests.py": tests_py}


# --------------------------------------------------------------------------
# Look & feel
# --------------------------------------------------------------------------
UI = {
    "bg": "#f3f4f6",
    "card": "#ffffff",
    "border": "#d9dce1",
    "text": "#1f2937",
    "muted": "#6b7280",
    "accent": "#2563eb",
    "accent_dark": "#1d4ed8",
    "tab": "#e5e7eb",
}

THEME = {            # code editor (VS Code "Dark+" colors)
    "bg": "#1e1e1e",
    "fg": "#d4d4d4",
    "cursor": "#aeafad",
    "select": "#264f78",
    "header": "#2d2d30",
    "header_fg": "#9da0a6",
}

TAG_COLORS = {
    "keyword": "#569cd6",   # class, def, return, import, export, const ...
    "class": "#4ec9b0",     # class names, React components, types ...
    "function": "#dcdcaa",  # function names, calls, decorators
    "string": "#ce9178",
    "number": "#b5cea8",
    "comment": "#6a9955",
    "variable": "#9cdcfe",  # field names, attributes
    "constant": "#4fc1ff",  # PROTECT, CASCADE ...
    "jsx_tag": "#808080",   # JSX element brackets/tags
}

METHOD_COLORS = {"GET": "#2563eb", "POST": "#16a34a", "PUT": "#d97706",
                 "PATCH": "#7c3aed", "DELETE": "#dc2626"}


def status_color(code):
    return {"2": "#16a34a", "4": "#d97706", "5": "#dc2626"}.get(str(code)[:1],
                                                                  UI["muted"])


def ui_font(size=10, bold=False):
    fam = ("Segoe UI" if sys.platform == "win32"
           else "Helvetica Neue" if sys.platform == "darwin" else "DejaVu Sans")
    return (fam, size, "bold") if bold else (fam, size)


def mono_font(size=None):
    if sys.platform == "win32":
        return ("Consolas", size or 11)
    if sys.platform == "darwin":
        return ("Menlo", size or 12)
    return ("DejaVu Sans Mono", size or 11)


# --------------------------------------------------------------------------
# Syntax highlighting
# --------------------------------------------------------------------------
_JS_KW = {"import", "export", "default", "from", "function", "const", "let", "var",
          "async", "await", "return", "if", "else", "try", "catch", "finally",
          "throw", "new", "null", "undefined", "true", "false", "typeof"}
_KEYWORDS = set(keyword.kwlist) | _JS_KW

_STRING = (
    r"(?:[rRbBfFuU]{1,2})?"
    r'(?:"""[\s\S]*?(?:"""|\Z)'
    r"|'''[\s\S]*?(?:'''|\Z)"
    r'|"(?:\\.|[^"\\\n])*(?:"|$)'
    r"|'(?:\\.|[^'\\\n])*(?:'|$)"
    r"|`(?:\\.|[^`\\])*(?:`|$))"
)
TOKEN_RE = re.compile(
    r"(?P<comment>#[^\n]*|//[^\n]*)"
    r"|(?P<string>" + _STRING + r")"
    r"|(?P<jsx_tag></?[A-Za-z0-9_\-]+>)"
    r"|(?P<decorator>@[A-Za-z_][\w.]*)"
    r"|(?P<number>\b\d[\d_]*(?:\.\d+)?\b)"
    r"|(?P<name>[A-Za-z_]\w*)",
    re.MULTILINE,
)

JSON_RE = re.compile(
    r'(?P<key>"(?:\\.|[^"\\])*")(?=\s*:)'
    r'|(?P<str>"(?:\\.|[^"\\])*")'
    r'|(?P<num>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)'
    r'|(?P<lit>\btrue\b|\bfalse\b|\bnull\b)'
)
JSON_TAGS = {"key": "variable", "str": "string", "num": "number", "lit": "keyword"}


def tokenize_python(text):
    """Yield (start_offset, end_offset, tag) for every colorable token."""
    prev_word = None
    for m in TOKEN_RE.finditer(text):
        kind = m.lastgroup
        s, e = m.span()
        if kind != "name":
            prev_word = None
            if kind == "decorator":
                yield s, e, "function"
            elif kind == "jsx_tag":
                yield s, e, "jsx_tag"
            else:
                yield s, e, {"comment": "comment", "string": "string",
                             "number": "number"}[kind]
            continue

        word = m.group()
        after = text[e:e + 80].lstrip(" \t")
        if word in _KEYWORDS or word in ("self", "cls"):
            tag = "keyword"
        elif prev_word in ("class", "function"):
            tag = "class" if word[0].isupper() else "function"
        elif after.startswith("("):
            tag = "class" if word[0].isupper() else "function"
        elif word.isupper() and len(word) > 1:
            tag = "constant"
        elif word[0].isupper() or after.startswith("."):
            tag = "class"
        else:
            tag = "variable"
        prev_word = word if word in ("class", "def", "function") else None
        yield s, e, tag


def setup_tags(widget):
    for tag, color in TAG_COLORS.items():
        widget.tag_configure(tag, foreground=color)
    widget.tag_raise("sel")


def apply_highlight(widget):
    text = widget.get("1.0", "end-1c")
    for tag in TAG_COLORS:
        widget.tag_remove(tag, "1.0", "end")

    line_starts = [0] + [m.end() for m in re.finditer("\n", text)]

    def pos(offset):
        i = bisect.bisect_right(line_starts, offset) - 1
        return f"{i + 1}.{offset - line_starts[i]}"

    for s, e, tag in tokenize_python(text):
        widget.tag_add(tag, pos(s), pos(e))


def apply_json_highlight(widget):
    text = widget.get("1.0", "end-1c")
    for m in JSON_RE.finditer(text):
        widget.tag_add(JSON_TAGS[m.lastgroup], f"1.0+{m.start()}c", f"1.0+{m.end()}c")


# --------------------------------------------------------------------------
# Reusable widgets
# --------------------------------------------------------------------------
def make_editor(parent, readonly=False):
    """A dark code Text widget with scrollbars. Returns the Text widget."""
    frame = ttk.Frame(parent)
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    txt = tk.Text(
        frame, wrap="none", undo=not readonly, font=mono_font(),
        width=1,
        bg=THEME["bg"], fg=THEME["fg"], insertbackground=THEME["cursor"],
        selectbackground=THEME["select"], relief="flat", padx=10, pady=8,
        tabs=("1c",), highlightthickness=0,
    )
    sy = ttk.Scrollbar(frame, orient="vertical", command=txt.yview)
    sx = ttk.Scrollbar(frame, orient="horizontal", command=txt.xview)
    txt.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
    txt.grid(row=0, column=0, sticky="nsew")
    sy.grid(row=0, column=1, sticky="ns")
    sx.grid(row=1, column=0, sticky="ew")
    setup_tags(txt)
    txt.container = frame
    return txt


class ScrollFrame(tk.Frame):
    """A vertically scrolling container; put widgets into `.inner`."""

    def __init__(self, parent):
        super().__init__(parent, bg=UI["bg"])
        self.canvas = tk.Canvas(self, bg=UI["bg"], highlightthickness=0)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=UI["bg"])
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(
            self._win, width=e.width))

    def clear(self):
        for child in self.inner.winfo_children():
            child.destroy()
        self.canvas.yview_moveto(0)
        tk.Frame(self.inner, bg=UI["bg"], height=10).pack()

    def scroll(self, units):
        if self.canvas.yview() != (0.0, 1.0):
            self.canvas.yview_scroll(units, "units")


def make_card(parent):
    outer = tk.Frame(parent, bg=UI["bg"])
    outer.pack(fill="x", padx=14, pady=(0, 12))
    body = tk.Frame(outer, bg=UI["card"], highlightbackground=UI["border"],
                    highlightthickness=1)
    body.pack(fill="x")
    return body


def badge(parent, text, color, width=None):
    return tk.Label(parent, text=text, bg=color, fg="white", padx=8, pady=2,
                    font=ui_font(9, True), width=width)


def note_label(parent, text, fg=None, bg=None, size=10):
    bg = bg or UI["card"]
    lbl = tk.Label(parent, text=text, bg=bg, fg=fg or UI["text"], justify="left",
                   anchor="w", font=ui_font(size), wraplength=500)
    lbl.bind("<Configure>", lambda e: lbl.config(wraplength=max(200, e.width - 4)))
    return lbl


def section_label(parent, text):
    tk.Label(parent, text=text, bg=UI["card"], fg=UI["muted"], anchor="w",
             font=ui_font(8, True)).pack(fill="x", padx=14, pady=(10, 4))


def code_block(parent, text, caption="json", highlight=True):
    """Dark, read-only code box with a Copy link in its header."""
    frame = tk.Frame(parent, bg=THEME["bg"])
    head = tk.Frame(frame, bg=THEME["header"])
    head.pack(fill="x")
    tk.Label(head, text=caption, bg=THEME["header"], fg=THEME["header_fg"],
             font=ui_font(9)).pack(side="left", padx=10, pady=3)
    copy = tk.Label(head, text="Copy", bg=THEME["header"], fg=TAG_COLORS["variable"],
                    cursor="hand2", font=ui_font(9, True))
    copy.pack(side="right", padx=10)

    def do_copy(_event=None):
        frame.clipboard_clear()
        frame.clipboard_append(text)
        copy.config(text="Copied!")
        frame.after(1200, lambda: copy.winfo_exists() and copy.config(text="Copy"))

    copy.bind("<Button-1>", do_copy)

    txt = tk.Text(frame, height=text.count("\n") + 1, width=1, wrap="none",
                  font=mono_font(10), bg=THEME["bg"], fg=THEME["fg"],
                  selectbackground=THEME["select"], relief="flat", padx=12,
                  pady=8, highlightthickness=0, cursor="arrow")
    txt.pack(fill="x")
    setup_tags(txt)
    txt.insert("1.0", text)
    if highlight:
        apply_json_highlight(txt)
    txt.config(state="disabled")
    return frame


def render_endpoint(parent, ep):
    card = make_card(parent)
    head = tk.Frame(card, bg=UI["card"])
    head.pack(fill="x", padx=14, pady=(12, 2))
    badge(head, ep["method"], METHOD_COLORS.get(ep["method"], UI["muted"]),
          width=8).pack(side="left")
    tk.Label(head, text=ep["path"], bg=UI["card"], fg=UI["text"],
             font=mono_font(11)).pack(side="left", padx=10)
    tk.Label(head, text=ep["title"], bg=UI["card"], fg=UI["muted"],
             font=ui_font(10)).pack(side="right")

    bits = []
    if ep.get("content_type"):
        bits.append("Content-Type: " + ep["content_type"])
    if ep.get("note"):
        bits.append(ep["note"])
    if bits:
        note_label(card, "  ·  ".join(bits), fg=UI["muted"], size=9).pack(
            fill="x", padx=14, pady=(2, 0))

    if ep.get("query"):
        section_label(card, "QUERY PARAMETERS")
        code_block(card, f"GET {ep['path']}{ep['query_example']}", "example",
                   highlight=False).pack(fill="x", padx=14)
        grid = tk.Frame(card, bg=UI["card"])
        grid.pack(fill="x", padx=14, pady=(8, 0))
        for r, q in enumerate(ep["query"]):
            tk.Label(grid, text=q["param"], bg=UI["card"], fg=UI["accent"],
                     font=mono_font(10), anchor="w").grid(
                row=r, column=0, sticky="w", padx=(0, 18), pady=1)
            tk.Label(grid, text=q["desc"], bg=UI["card"], fg=UI["text"],
                     font=ui_font(9), anchor="w").grid(
                row=r, column=1, sticky="w", pady=1)
            tk.Label(grid, text="e.g. " + q["example"], bg=UI["card"],
                     fg=UI["muted"], font=mono_font(9), anchor="w").grid(
                row=r, column=2, sticky="w", padx=(18, 0), pady=1)

    if ep["request"] is not None:
        section_label(card, "REQUEST BODY")
        code_block(card, _jd(ep["request"]), "json").pack(fill="x", padx=14)

    section_label(card, "RESPONSES")
    for r in ep["responses"]:
        row = tk.Frame(card, bg=UI["card"])
        row.pack(fill="x", padx=14, pady=(6, 3))
        badge(row, str(r["status"]), status_color(r["status"]), width=5).pack(side="left")
        tk.Label(row, text=r["text"], bg=UI["card"], fg=UI["text"],
                 font=ui_font(10)).pack(side="left", padx=8)
        if r["body"] is not None:
            code_block(card, _jd(r["body"]), "json").pack(fill="x", padx=14)
    tk.Frame(card, bg=UI["card"], height=12).pack()


def render_ws_message(parent, m):
    card = make_card(parent)
    head = tk.Frame(card, bg=UI["card"])
    head.pack(fill="x", padx=14, pady=(12, 2))
    badge(head, m["title"].upper(), "#7c3aed", width=10).pack(side="left")
    tk.Label(head, text=m["summary"], bg=UI["card"], fg=UI["muted"],
             font=ui_font(10)).pack(side="left", padx=10)

    section_label(card, "YOU SEND")
    code_block(card, _jd(m["send"]), "json").pack(fill="x", padx=14)
    section_label(card, "YOU RECEIVE")
    code_block(card, _jd(m["receive"]), "json").pack(fill="x", padx=14)
    if m["push"] is not None:
        section_label(card, "OTHER CONNECTED CLIENTS RECEIVE")
        code_block(card, _jd(m["push"]), "json").pack(fill="x", padx=14)
    tk.Frame(card, bg=UI["card"], height=12).pack()


def render_ws(parent, ws):
    card = make_card(parent)
    head = tk.Frame(card, bg=UI["card"])
    head.pack(fill="x", padx=14, pady=(12, 2))
    badge(head, "CONNECT", "#0f766e", width=10).pack(side="left")
    tk.Label(head, text="Open one socket per model", bg=UI["card"], fg=UI["muted"],
             font=ui_font(10)).pack(side="left", padx=10)
    code_block(card, ws["url"], "url", highlight=False).pack(fill="x", padx=14,
                                                              pady=(8, 4))
    for n in ws["notes"]:
        note_label(card, "•  " + n, fg=UI["muted"], size=9).pack(fill="x", padx=14,
                                                                  pady=1)
    tk.Frame(card, bg=UI["card"], height=12).pack()

    for m in ws["messages"]:
        render_ws_message(parent, m)

    card = make_card(parent)
    head = tk.Frame(card, bg=UI["card"])
    head.pack(fill="x", padx=14, pady=(12, 2))
    badge(head, "ERRORS", "#dc2626", width=10).pack(side="left")
    tk.Label(head, text="Every failure comes back as an error message",
             bg=UI["card"], fg=UI["muted"], font=ui_font(10)).pack(side="left", padx=10)
    for e in ws["errors"]:
        section_label(card, e["title"].upper())
        code_block(card, _jd(e["body"]), "json").pack(fill="x", padx=14)
    tk.Frame(card, bg=UI["card"], height=12).pack()


def empty_state(parent, title, hint):
    card = make_card(parent)
    tk.Label(card, text=title, bg=UI["card"], fg=UI["text"],
             font=ui_font(12, True)).pack(padx=20, pady=(24, 4))
    tk.Label(card, text=hint, bg=UI["card"], fg=UI["muted"],
             font=ui_font(10)).pack(padx=20, pady=(0, 24))


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------
class App(tk.Tk):
    PAYLOAD_TAB = "API Payloads"
    BACKEND_TAB = "Backend"
    FRONTEND_TAB = "Frontend"

    def __init__(self):
        super().__init__()
        self.title("DRF Generator")
        self.geometry("1480x900")
        self.minsize(1000, 560)
        self.configure(bg=UI["bg"])

        self.explicit = tk.BooleanVar(value=False)
        self.with_urls = tk.BooleanVar(value=True)
        self.optimize = tk.BooleanVar(value=True)
        self.gen_signals = tk.BooleanVar(value=True)
        self.gen_ws = tk.BooleanVar(value=True)
        self.gen_filters = tk.BooleanVar(value=True)
        self.gen_search = tk.BooleanVar(value=True)
        self.gen_ordering = tk.BooleanVar(value=True)
        self.signal_vars = {
            "pre_save": tk.BooleanVar(value=False),
            "post_save": tk.BooleanVar(value=True),
            "pre_delete": tk.BooleanVar(value=False),
            "post_delete": tk.BooleanVar(value=True),
        }
        self.permission = tk.StringVar(value="None")
        self.module = tk.StringVar(value=".models")
        self.outputs = {}
        self.docs = []
        self.scrollframes = []
        self.signal_buttons = []
        self._hl_job = None
        self._gen_job = None
        self.options_window = None

        self._style()
        self._build()
        self.input.insert("1.0", SAMPLE)
        apply_highlight(self.input)
        self.generate()

        # Re-generate live whenever an option changes.
        watched = [self.explicit, self.with_urls, self.optimize, self.gen_signals,
                   self.gen_ws, self.permission, self.module,
                   self.gen_filters, self.gen_search, self.gen_ordering,
                   *self.signal_vars.values()]
        for var in watched:
            var.trace_add("write", lambda *_: self.schedule_generate())
        self.gen_signals.trace_add("write", lambda *_: self._sync_signal_state())
        self._sync_signal_state()

        self.bind_all("<MouseWheel>", self._on_wheel)
        self.bind_all("<Button-4>", self._on_wheel)
        self.bind_all("<Button-5>", self._on_wheel)

    # ---- styling ---------------------------------------------------------
    def _style(self):
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure(".", background=UI["bg"], foreground=UI["text"], font=ui_font(10))
        s.configure("TFrame", background=UI["bg"])
        s.configure("TLabel", background=UI["bg"])
        s.configure("TCheckbutton", background=UI["bg"])
        s.configure("TLabelframe", background=UI["bg"], bordercolor=UI["border"])
        s.configure("TLabelframe.Label", background=UI["bg"], foreground=UI["muted"],
                    font=ui_font(9, True))
        s.configure("TNotebook", background=UI["bg"], borderwidth=0)
        s.configure("TNotebook.Tab", padding=(12, 6), background=UI["tab"],
                    foreground=UI["muted"], borderwidth=0)
        s.map("TNotebook.Tab",
              background=[("selected", UI["card"])],
              foreground=[("selected", UI["accent"])])
        s.configure("TButton", padding=(12, 6))
        s.configure("Accent.TButton", background=UI["accent"], foreground="white",
                    font=ui_font(10, True), padding=(16, 6), borderwidth=0)
        s.map("Accent.TButton", background=[("active", UI["accent_dark"])])
        s.configure("TEntry", padding=(7, 5))
        s.configure("TCombobox", padding=(7, 5))
        s.configure("TLabelframe", padding=(12, 8))
        s.configure("TNotebook.Tab", padding=(14, 8))
        s.configure("Treeview", rowheight=28, background=UI["card"],
                    fieldbackground=UI["card"], borderwidth=0)
        s.configure("Treeview.Heading", background=UI["tab"], font=ui_font(10, True),
                    relief="flat", padding=(6, 5))
        s.map("Treeview", background=[("selected", UI["accent"])],
              foreground=[("selected", "white")])

    # ---- layout ----------------------------------------------------------
    def _group(self, parent, title):
        lf = ttk.Labelframe(parent, text=title, padding=(10, 4, 10, 6))
        lf.pack(side="left", padx=(0, 10), fill="y")
        return lf

    def _build(self):
        header = ttk.Frame(self, padding=(18, 14, 18, 10))
        header.pack(fill="x")
        brand = ttk.Frame(header)
        brand.pack(side="left", fill="x", expand=True)
        ttk.Label(brand, text="DRF Generator",
                  font=ui_font(18, True)).pack(anchor="w")
        ttk.Label(
            brand,
            text="Turn Django models into ready-to-review API code and tests.",
            foreground=UI["muted"],
        ).pack(anchor="w", pady=(2, 0))
        actions = ttk.Frame(header)
        actions.pack(side="right")
        ttk.Button(actions, text="Options", command=self.show_options
                   ).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Generate", style="Accent.TButton",
                   command=self.generate).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Save all...", command=self.save_all
                   ).pack(side="left")

        panes = tk.PanedWindow(self, orient="horizontal", sashwidth=8,
                               sashrelief="flat", bg=UI["border"], bd=0)
        panes.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        left = ttk.Labelframe(panes, text="1  Django models", padding=10)
        right = ttk.Frame(panes)
        panes.add(left, minsize=360, width=510, stretch="always")
        panes.add(right, minsize=520, stretch="always")

        input_tools = ttk.Frame(left)
        input_tools.pack(fill="x", pady=(0, 8))
        ttk.Label(input_tools, text="Paste or type your model definitions.",
                  foreground=UI["muted"]).pack(side="left")
        self.model_count = ttk.Label(input_tools, text="0 models",
                                     foreground=UI["muted"])
        self.model_count.pack(side="right")
        self.input = make_editor(left)
        self.input.container.pack(fill="both", expand=True)
        self.input.bind("<<Modified>>", self._on_modified)
        side = ttk.Frame(left)
        side.pack(fill="x", pady=(8, 0))
        ttk.Button(side, text="Paste", command=self.paste_clipboard
                   ).pack(side="left")
        ttk.Button(side, text="Load example", command=self.load_sample
                   ).pack(side="left", padx=6)
        ttk.Button(side, text="Clear input", command=self.clear_input
                   ).pack(side="left")

        self.categories = ttk.Notebook(right)
        self.categories.pack(fill="both", expand=True)
        backend = ttk.Frame(self.categories)
        self.categories.add(backend, text="Backend files")
        self.tabs = ttk.Notebook(backend)
        self.tabs.pack(fill="both", expand=True)
        self.out_widgets = {}
        for fname in ("serializers.py", "views.py", "urls.py", "tests.py", "filters.py", "signals.py", "consumers.py", "routing.py", "channels_setup.py", "filtering_setup.py"):
            txt = make_editor(self.tabs, readonly=True)
            txt.config(state="disabled")
            self.tabs.add(txt.container, text=fname)
            self.out_widgets[fname] = txt
        pay = ttk.Frame(self.tabs)
        self.tabs.add(pay, text=self.PAYLOAD_TAB)
        self._build_payload_tab(pay)
        frontend = ttk.Frame(self.categories)
        self.categories.add(frontend, text="React examples")
        self._build_frontend_tab(frontend)

        bottom = ttk.Frame(self, padding=(12, 4, 12, 12))
        bottom.pack(fill="x")
        self.status = tk.Label(bottom, text="", bg=UI["bg"], fg=UI["muted"],
                               font=ui_font(10), anchor="e")
        self.status.pack(side="left", fill="x", expand=True)
        ttk.Button(bottom, text="Copy current tab", command=self.copy_current
                   ).pack(side="right")
        ttk.Label(bottom, text="Ctrl+Enter: generate   Ctrl+S: save",
                  foreground=UI["muted"]).pack(side="right", padx=(0, 14))

        self.bind("<Control-Return>", lambda e: self.generate())
        self.bind("<Control-s>", lambda e: self.save_all())
        self.bind("<Control-S>", lambda e: self.save_all())
        self.bind("<Control-Shift-c>", lambda e: self.copy_current())

    def show_options(self):
        if self.options_window and self.options_window.winfo_exists():
            self.options_window.deiconify()
            self.options_window.lift()
            return

        window = tk.Toplevel(self)
        self.options_window = window
        window.title("Generation options")
        window.transient(self)
        window.resizable(False, False)
        window.configure(bg=UI["bg"])
        window.protocol("WM_DELETE_WINDOW", window.destroy)

        body = ttk.Frame(window, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Generation options",
                  font=ui_font(16, True)).pack(anchor="w")
        ttk.Label(body, text="Changes are applied automatically.",
                  foreground=UI["muted"]).pack(anchor="w", pady=(2, 12))

        settings = ttk.Frame(body)
        settings.pack(fill="x")
        code = ttk.Labelframe(settings, text="Code")
        code.pack(fill="x", pady=(0, 10))
        ttk.Checkbutton(code, text="Optimize database queries",
                        variable=self.optimize).pack(anchor="w", pady=3)
        ttk.Checkbutton(code, text="Use an explicit serializer field list",
                        variable=self.explicit).pack(anchor="w", pady=3)
        ttk.Checkbutton(code, text="Generate urls.py",
                        variable=self.with_urls).pack(anchor="w", pady=3)

        access = ttk.Labelframe(settings, text="Access and imports")
        access.pack(fill="x", pady=(0, 10))
        ttk.Label(access, text="Default permission").grid(
            row=0, column=0, sticky="w", padx=(0, 10), pady=4)
        permission = ttk.Combobox(
            access, textvariable=self.permission, width=30, state="readonly",
            values=["None", "IsAuthenticated", "IsAuthenticatedOrReadOnly",
                    "IsAdminUser", "AllowAny"])
        permission.grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Label(access, text="Models import path").grid(
            row=1, column=0, sticky="w", padx=(0, 10), pady=4)
        ttk.Entry(access, textvariable=self.module, width=34).grid(
            row=1, column=1, sticky="ew", pady=4)
        access.columnconfigure(1, weight=1)

        optional = ttk.Labelframe(settings, text="Optional files")
        optional.pack(fill="x")
        ttk.Checkbutton(optional, text="WebSocket consumers and setup",
                        variable=self.gen_ws).pack(anchor="w", pady=3)
        ttk.Checkbutton(optional, text="Generate signal receivers",
                        variable=self.gen_signals).pack(anchor="w", pady=3)
        signals = ttk.Frame(optional)
        signals.pack(fill="x", padx=(20, 0), pady=(0, 4))
        self.signal_buttons = []
        for sig in SIGNAL_ORDER:
            cb = ttk.Checkbutton(signals, text=sig, variable=self.signal_vars[sig])
            cb.pack(side="left", padx=(0, 10))
            self.signal_buttons.append(cb)

        filtering = ttk.Frame(optional)
        filtering.pack(fill="x", pady=(4, 0))
        ttk.Checkbutton(filtering, text="Filters",
                        variable=self.gen_filters).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(filtering, text="Search",
                        variable=self.gen_search).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(filtering, text="Ordering",
                        variable=self.gen_ordering).pack(side="left")

        ttk.Button(body, text="Done", command=window.destroy
                   ).pack(anchor="e", pady=(12, 0))
        window.update_idletasks()
        x = self.winfo_rootx() + max(0, (self.winfo_width() - window.winfo_width()) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - window.winfo_height()) // 2)
        window.geometry(f"+{x}+{y}")
        window.grab_set()
        self._sync_signal_state()

    def _build_payload_tab(self, parent):
        top = ttk.Frame(parent, padding=(10, 10, 10, 6))
        top.pack(fill="x")
        ttk.Label(top, text="Model").pack(side="left")
        self.model_var = tk.StringVar()
        self.model_combo = ttk.Combobox(top, textvariable=self.model_var,
                                        state="readonly", width=28)
        self.model_combo.pack(side="left", padx=8)
        self.model_combo.bind("<<ComboboxSelected>>", lambda e: self.render_payloads())
        self.summary = ttk.Label(top, text="", foreground=UI["muted"])
        self.summary.pack(side="left", padx=8)

        self.ptabs = ttk.Notebook(parent)
        self.ptabs.pack(fill="both", expand=True)

        fields_frame = ttk.Frame(self.ptabs)
        fields_frame.rowconfigure(0, weight=1)
        fields_frame.columnconfigure(0, weight=1)
        cols = ("field", "type", "required", "notes")
        self.tree = ttk.Treeview(fields_frame, columns=cols, show="headings",
                                 selectmode="browse")
        for col, title, width in (("field", "Field", 170), ("type", "Type", 190),
                                  ("required", "Required", 90), ("notes", "Notes", 360)):
            self.tree.heading(col, text=title, anchor="w")
            self.tree.column(col, width=width, anchor="w")
        self.tree.tag_configure("odd", background="#f9fafb")
        self.tree.tag_configure("req", foreground="#b45309")
        sy = ttk.Scrollbar(fields_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sy.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sy.grid(row=0, column=1, sticky="ns")

        self.rest_sf = ScrollFrame(self.ptabs)
        self.ws_sf = ScrollFrame(self.ptabs)
        self.scrollframes += [self.rest_sf, self.ws_sf]
        self.ptabs.add(fields_frame, text="Fields")
        self.ptabs.add(self.rest_sf, text="REST API")
        self.ptabs.add(self.ws_sf, text="WebSocket")

    # ---- small helpers ---------------------------------------------------
    def set_status(self, text, kind="ok"):
        color = {"ok": "#15803d", "error": "#b91c1c"}.get(kind, UI["muted"])
        self.status.config(text=text, fg=color)

    def _sync_signal_state(self):
        state = "normal" if self.gen_signals.get() else "disabled"
        for cb in self.signal_buttons:
            cb.config(state=state)

    def _on_wheel(self, event):
        if event.num == 4:
            units = -3
        elif event.num == 5:
            units = 3
        elif sys.platform == "darwin":
            units = -event.delta
        else:
            units = -int(event.delta / 120) * 3
        try:
            w = self.winfo_containing(event.x_root, event.y_root)
        except (KeyError, tk.TclError):
            return
        while w is not None:
            for sf in self.scrollframes:
                if w is sf.canvas:
                    sf.scroll(units)
                    return
            w = w.master

    # ---- input actions ---------------------------------------------------
    def _on_modified(self, _event=None):
        if self.input.edit_modified():
            self.input.edit_modified(False)
            if self._hl_job:
                self.after_cancel(self._hl_job)
            self._hl_job = self.after(120, lambda: apply_highlight(self.input))
            self.schedule_generate()

    def schedule_generate(self):
        if self._gen_job:
            self.after_cancel(self._gen_job)
        self._gen_job = self.after(350, self.generate)

    def paste_clipboard(self):
        try:
            text = self.clipboard_get()
        except tk.TclError:
            self.set_status("Clipboard is empty.", "error")
            return
        self.input.delete("1.0", "end")
        self.input.insert("1.0", text)

    def load_sample(self):
        self.input.delete("1.0", "end")
        self.input.insert("1.0", SAMPLE)

    def clear_input(self):
        self.input.delete("1.0", "end")

    # ---- generation ------------------------------------------------------
    def generate(self):
        if self._gen_job:
            self.after_cancel(self._gen_job)
            self._gen_job = None
        src = self.input.get("1.0", "end")
        try:
            models = parse_models(src)
            details = parse_field_details(src)
        except SyntaxError as e:
            self._clear_generated_outputs(
                f"Syntax error on line {e.lineno}: {e.msg}")
            return
        except ValueError as e:
            self._clear_generated_outputs(f"Cannot read the models: {e}")
            return
        if not models:
            self._clear_generated_outputs(
                "No Django models found. Paste classes that extend models.Model.")
            return

        permission = self.permission.get()
        self.outputs = generate(
            models,
            explicit_fields=self.explicit.get(),
            permission=permission,
            with_urls=self.with_urls.get(),
            models_module=self.module.get().strip() or ".models",
            optimize=self.optimize.get(),
            websocket=self.gen_ws.get(),
            signals=[sig for sig, v in self.signal_vars.items() if v.get()]
            if self.gen_signals.get() else [],
            details=details,
            filtering=self.gen_filters.get(),
            search=self.gen_search.get(),
            ordering=self.gen_ordering.get(),
        )
        self.docs = build_payload_docs(
            models, details, permission, self.gen_ws.get(),
            filtering=self.gen_filters.get(), search=self.gen_search.get(),
            ordering=self.gen_ordering.get())

        for fname, widget in self.out_widgets.items():
            widget.config(state="normal")
            widget.delete("1.0", "end")
            widget.insert("1.0", self.outputs.get(fname) or
                          "# Not generated. Turn it on in the options above.\n")
            apply_highlight(widget)
            widget.config(state="disabled")

        names = [d["name"] for d in self.docs]
        self.model_combo["values"] = names
        if self.model_var.get() not in names:
            self.model_var.set(names[0])
        self.render_payloads()
        self.render_frontend()
        self.model_count.config(
            text=f"{len(models)} model{'s' if len(models) != 1 else ''}")
        self.set_status(f"Generated for {len(models)} model(s): {', '.join(names)}")

    def _clear_generated_outputs(self, message):
        self.outputs = {}
        self.docs = []
        self.model_count.config(text="0 models")
        for fname, widget in self.out_widgets.items():
            widget.config(state="normal")
            widget.delete("1.0", "end")
            widget.insert("1.0", "# " + message + "\n")
            widget.config(state="disabled")
        self.model_combo["values"] = ()
        self.frontend_model_combo["values"] = ()
        self.render_payloads()
        self.render_frontend()
        self.set_status(message, "error")

    def current_doc(self):
        return next((d for d in self.docs if d["name"] == self.model_var.get()), None)

    def render_payloads(self):
        doc = self.current_doc()
        self.tree.delete(*self.tree.get_children())
        self.rest_sf.clear()
        self.ws_sf.clear()
        if not doc:
            return

        ws_count = len(doc["ws"]["messages"]) if doc["ws"] else 0
        self.summary.config(text=f"{len(doc['rest'])} REST endpoints  ·  "
                                 f"{ws_count} WebSocket actions  ·  "
                                 f"/{doc['route']}/")

        for n, f in enumerate(doc["fields"]):
            tags = ["odd"] if n % 2 else []
            if f["required"] == "yes":
                tags.append("req")
            self.tree.insert("", "end", values=(f["name"], f["type"], f["required"],
                                                f["notes"]), tags=tags)

        card = make_card(self.rest_sf.inner)
        tk.Label(card, text="How to read this", bg=UI["card"], fg=UI["text"],
                 font=ui_font(10, True), anchor="w").pack(fill="x", padx=14,
                                                          pady=(10, 2))
        for n in doc["rest_notes"]:
            note_label(card, "•  " + n, fg=UI["muted"], size=9).pack(fill="x", padx=14,
                                                                      pady=1)
        tk.Frame(card, bg=UI["card"], height=10).pack()
        for ep in doc["rest"]:
            render_endpoint(self.rest_sf.inner, ep)

        if doc["ws"]:
            render_ws(self.ws_sf.inner, doc["ws"])
        else:
            empty_state(self.ws_sf.inner, "WebSocket is turned off",
                        "Enable \"Consumers, routing, setup\" in the options above "
                        "to see the WebSocket payloads.")

    def _build_frontend_tab(self, parent):
        top = ttk.Frame(parent, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="Model").pack(side="left")
        self.frontend_model_var = tk.StringVar()
        self.frontend_model_combo = ttk.Combobox(top, textvariable=self.frontend_model_var, state="readonly", width=28)
        self.frontend_model_combo.pack(side="left", padx=8)
        self.frontend_model_combo.bind("<<ComboboxSelected>>", lambda _e: self.render_frontend())
        self.frontend_tabs = ttk.Notebook(parent)
        self.frontend_tabs.pack(fill="both", expand=True)
        self.frontend_widgets = {}
        for label in ("List", "Card", "Create Form", "Update Form"):
            editor = make_editor(self.frontend_tabs, readonly=True)
            editor.config(state="disabled")
            self.frontend_tabs.add(editor.container, text=label)
            self.frontend_widgets[label] = editor

    def _react_samples(self, doc):
        model, route = doc["name"], doc["route"]
        api_url = f"http://localhost:8000/api/{route}/"
        writable = [f for f in doc["fields"] if f["name"] != "id" and f["required"] != "read-only"]
        display = [f["name"] for f in doc["fields"] if f["name"] != "id"]
        primary = display[0] if display else "id"

        list_code = f'''import {{ useEffect, useState }} from "react";

const API_URL = "{api_url}";

export default function {model}List() {{
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {{
    const controller = new AbortController();

    fetch(API_URL, {{ signal: controller.signal }})
      .then((res) => {{
        if (!res.ok) throw new Error(`HTTP ${{res.status}}`);
        return res.json();
      }})
      .then((data) => setItems(Array.isArray(data) ? data : (data.results ?? [])))
      .catch((err) => {{
        if (err.name !== "AbortError") setError(err.message);
      }})
      .finally(() => setLoading(false));

    return () => controller.abort();
  }}, []);

  if (loading) return <p className="loading">Loading {plural(model).lower()}...</p>;
  if (error) return <p role="alert" className="error-message">{{error}}</p>;

  return (
    <section className="{snake(model)}-list-container">
      <h1>{model} List</h1>
      <ul>
        {{items.map((item) => (
          <li key={{item.id}}>
            {{item["{primary}"] ?? `Record #${{item.id}}`}}
          </li>
        ))}}
      </ul>
    </section>
  );
}}
'''

        card_fields = "\n".join(
            f'          <p><strong>{name.replace("_", " ").title()}:</strong> {{String(item["{name}"] ?? "—")}}</p>'
            for name in display[:8]
        )

        card_code = f'''import {{ useEffect, useState }} from "react";

const API_URL = "{api_url}";

export default function {model}Cards() {{
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {{
    const controller = new AbortController();

    fetch(API_URL, {{ signal: controller.signal }})
      .then((res) => {{
        if (!res.ok) throw new Error(`HTTP ${{res.status}}`);
        return res.json();
      }})
      .then((data) => setItems(Array.isArray(data) ? data : (data.results ?? [])))
      .catch((err) => {{
        if (err.name !== "AbortError") setError(err.message);
      }})
      .finally(() => setLoading(false));

    return () => controller.abort();
  }}, []);

  if (loading) return <p className="loading">Loading cards...</p>;
  if (error) return <p role="alert" className="error-message">{{error}}</p>;

  return (
    <section className="record-grid">
      <h1>{model} Cards</h1>
      <div className="card-container">
        {{items.map((item) => (
          <article className="record-card" key={{item.id}}>
            <h2>{{item["{primary}"] ?? `Record #${{item.id}}`}}</h2>
{card_fields}
          </article>
        ))}}
      </div>
      <style>{{`
        .record-grid {{ padding: 1rem; }}
        .card-container {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 1rem; }}
        .record-card {{ border: 1px solid #e5e7eb; border-radius: 8px; padding: 1rem; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        .record-card h2 {{ margin-top: 0; font-size: 1.25rem; }}
        .record-card p {{ margin: 0.4rem 0; color: #374151; }}
      `}}</style>
    </section>
  );
}}
'''

        initial_state_lines = ",\n".join(f'  "{f["name"]}": ""' for f in writable)

        create_form_inputs = []
        for f in writable:
            name = f["name"]
            typ = ("number" if f["type"] in ("integer", "number", "decimal (string)")
                   else "date" if f["type"] == "date (YYYY-MM-DD)"
                   else "email" if "email" in name
                   else "text")
            req = "required" if f["required"] == "yes" else ""
            create_form_inputs.append(
                f'      <div className="form-group">\n'
                f'        <label htmlFor="{name}">{name.replace("_", " ").title()}</label>\n'
                f'        <input\n'
                f'          id="{name}"\n'
                f'          name="{name}"\n'
                f'          type="{typ}"\n'
                f'          value={{form["{name}"] ?? ""}}\n'
                f'          onChange={{handleChange}}\n'
                f'          {req}\n'
                f'        />\n'
                f'      </div>'
            )

        create_inputs_str = "\n".join(create_form_inputs)

        create_form_code = f'''import {{ useState }} from "react";

const API_URL = "{api_url}";

const INITIAL_FORM = {{
{initial_state_lines}
}};

export default function Create{model}Form() {{
  const [form, setForm] = useState(INITIAL_FORM);
  const [message, setMessage] = useState("");
  const [errors, setErrors] = useState(null);
  const [submitting, setSubmitting] = useState(false);

  function handleChange(e) {{
    const {{ name, value, type, checked }} = e.target;
    setForm((prev) => ({{
      ...prev,
      [name]: type === "checkbox" ? checked : type === "number" && value !== "" ? Number(value) : value,
    }}));
  }}

  async function handleSubmit(e) {{
    e.preventDefault();
    setMessage("");
    setErrors(null);
    setSubmitting(true);

    try {{
      const response = await fetch(API_URL, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(form),
      }});
      const data = await response.json().catch(() => ({{}}));

      if (!response.ok) {{
        setErrors(data);
        throw new Error("Creation failed. Please review input fields.");
      }}

      setMessage("Created successfully.");
      setForm(INITIAL_FORM);
    }} catch (err) {{
      setMessage(err.message);
    }} finally {{
      setSubmitting(false);
    }}
  }}

  return (
    <form className="model-form" onSubmit={{handleSubmit}}>
      <h1>Create {model}</h1>
{create_inputs_str}
      <button type="submit" disabled={{submitting}}>
        {{submitting ? "Submitting..." : "Create"}}
      </button>

      {{message && <p role="status" className="status-msg">{{message}}</p>}}
      {{errors && <pre role="alert" className="error-box">{{JSON.stringify(errors, null, 2)}}</pre>}}
    </form>
  );
}}
'''

        update_form_inputs = []
        for f in writable:
            name = f["name"]
            typ = ("number" if f["type"] in ("integer", "number", "decimal (string)")
                   else "date" if f["type"] == "date (YYYY-MM-DD)"
                   else "email" if "email" in name
                   else "text")
            req = "required" if f["required"] == "yes" else ""
            update_form_inputs.append(
                f'      <div className="form-group">\n'
                f'        <label htmlFor="{name}">{name.replace("_", " ").title()}</label>\n'
                f'        <input\n'
                f'          id="{name}"\n'
                f'          name="{name}"\n'
                f'          type="{typ}"\n'
                f'          value={{form["{name}"] ?? ""}}\n'
                f'          onChange={{handleChange}}\n'
                f'          {req}\n'
                f'        />\n'
                f'      </div>'
            )

        update_inputs_str = "\n".join(update_form_inputs)

        update_form_code = f'''import {{ useEffect, useState }} from "react";

const API_URL = "{api_url}";

const INITIAL_FORM = {{
{initial_state_lines}
}};

export default function Update{model}Form({{ id }}) {{
  const [form, setForm] = useState(INITIAL_FORM);
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState("");
  const [errors, setErrors] = useState(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {{
    if (!id) return;
    const controller = new AbortController();

    fetch(`${{API_URL}}${{id}}/`, {{ signal: controller.signal }})
      .then((res) => {{
        if (!res.ok) throw new Error(`HTTP ${{res.status}}`);
        return res.json();
      }})
      .then((data) => {{
        const updatedState = {{}};
        Object.keys(INITIAL_FORM).forEach((key) => {{
          updatedState[key] = data[key] ?? "";
        }});
        setForm(updatedState);
      }})
      .catch((err) => {{
        if (err.name !== "AbortError") setMessage(`Failed to load: ${{err.message}}`);
      }})
      .finally(() => setLoading(false));

    return () => controller.abort();
  }}, [id]);

  function handleChange(e) {{
    const {{ name, value, type, checked }} = e.target;
    setForm((prev) => ({{
      ...prev,
      [name]: type === "checkbox" ? checked : type === "number" && value !== "" ? Number(value) : value,
    }}));
  }}

  async function handleSubmit(e) {{
    e.preventDefault();
    setMessage("");
    setErrors(null);
    setSubmitting(true);

    try {{
      const response = await fetch(`${{API_URL}}${{id}}/`, {{
        method: "PUT",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(form),
      }});
      const data = await response.json().catch(() => ({{}}));

      if (!response.ok) {{
        setErrors(data);
        throw new Error("Update failed. Please check form validation errors.");
      }}

      setMessage("Updated successfully!");
    }} catch (err) {{
      setMessage(err.message);
    }} finally {{
      setSubmitting(false);
    }}
  }}

  if (loading) return <p className="loading">Loading record details...</p>;

  return (
    <form className="model-form" onSubmit={{handleSubmit}}>
      <h1>Update {model} #{id}</h1>
{update_inputs_str}
      <button type="submit" disabled={{submitting}}>
        {{submitting ? "Saving..." : "Update {model}"}}
      </button>

      {{message && <p role="status" className="status-msg">{{message}}</p>}}
      {{errors && <pre role="alert" className="error-box">{{JSON.stringify(errors, null, 2)}}</pre>}}
    </form>
  );
}}
'''

        return {
            "List": list_code,
            "Card": card_code,
            "Create Form": create_form_code,
            "Update Form": update_form_code,
        }

    def render_frontend(self):
        names = [d["name"] for d in self.docs]
        self.frontend_model_combo["values"] = names
        if not names:
            for widget in self.frontend_widgets.values():
                widget.config(state="normal")
                widget.delete("1.0", "end")
                widget.config(state="disabled")
            for key in list(self.outputs):
                if key.startswith("frontend/"):
                    del self.outputs[key]
            return
        if self.frontend_model_var.get() not in names:
            self.frontend_model_var.set(names[0])
        doc = next((d for d in self.docs if d["name"] == self.frontend_model_var.get()), None)
        if not doc:
            return
        for label, code in self._react_samples(doc).items():
            widget = self.frontend_widgets[label]
            widget.config(state="normal")
            widget.delete("1.0", "end")
            widget.insert("1.0", code)
            apply_highlight(widget)
            widget.config(state="disabled")
            self.outputs[f"frontend/{label.replace(' ', '')}.jsx"] = code

    # ---- output actions --------------------------------------------------
    def copy_current(self):
        if self.categories.index(self.categories.select()) == 1:
            label = self.frontend_tabs.tab(self.frontend_tabs.select(), "text")
            content = self.outputs.get(f"frontend/{label.replace(' ', '')}.jsx", "")
            label = f"React {label}"
        else:
            fname = self.tabs.tab(self.tabs.select(), "text")
            if fname == self.PAYLOAD_TAB:
                doc = self.current_doc()
                if not doc: return
                part = self.ptabs.tab(self.ptabs.select(), "text")
                data = {"Fields": doc["fields"], "REST API": doc["rest"], "WebSocket": doc["ws"]}.get(part)
                content = _jd(data)
                label = f"{doc['name']} {part}"
            else:
                content = self.outputs.get(fname, "")
                label = fname
        if not content: return
        self.clipboard_clear()
        self.clipboard_append(content)
        self.set_status(f"Copied {label}")

    def save_all(self):
        if not self.outputs:
            self.generate()
        if not self.outputs:
            return
        folder = filedialog.askdirectory(title="Choose your Django app folder")
        if not folder:
            return
        files = {k: v for k, v in self.outputs.items()
                 if v and k not in SNIPPET_ONLY and not k.startswith("frontend/")}
        files["payloads.json"] = _jd(self.docs)
        frontend_files = {k.split("/", 1)[1]: v for k, v in self.outputs.items()
                          if v and k.startswith("frontend/")}
        written = []
        for fname, content in files.items():
            path = os.path.join(folder, fname)
            if os.path.exists(path) and not messagebox.askyesno(
                    "Overwrite?", f"{fname} already exists. Overwrite it?"):
                continue
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            written.append(fname)
        for fname, content in frontend_files.items():
            path = os.path.join(folder, "frontend", fname)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.exists(path) and not messagebox.askyesno("Overwrite?", f"frontend/{fname} already exists. Overwrite it?"):
                continue
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            written.append(f"frontend/{fname}")
        self.set_status(f"Saved: {', '.join(written) or 'nothing'}")


if __name__ == "__main__":
    App().mainloop()