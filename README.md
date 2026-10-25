<img width="1401" height="847" alt="image" src="https://github.com/user-attachments/assets/6fc80be7-10a6-420a-9db2-8b7b1eff0de9" />
<img width="1900" height="1020" alt="image" src="https://github.com/user-attachments/assets/b7b59f62-b35c-4274-9e65-e18b507210ff" />
<img width="1919" height="1032" alt="image" src="https://github.com/user-attachments/assets/27cac2a6-bc33-4874-b126-989e6775ddb9" />
# DRF Generator

A small desktop tool that turns pasted Django models into Django REST Framework boilerplate: serializers, views, URLs, filters, signals and WebSocket consumers. It also builds a readable API reference showing what a frontend sends and receives for every REST endpoint and WebSocket action.

It is a single Python file that uses only the standard library (Tkinter). Django does **not** need to be installed, because models are parsed with the `ast` module and never imported.

## Features

- **Serializers**: a `ModelSerializer` per model, with `__all__` or an explicit field list.
- **Views**: `ListCreateAPIView` and `RetrieveUpdateDestroyAPIView` per model, with optional `select_related` / `prefetch_related` to avoid N+1 queries.
- **URLs**: routes with the right converter for the primary key (`int`, `uuid` or `str`).
- **Permissions**: `IsAuthenticated`, `IsAuthenticatedOrReadOnly`, `IsAdminUser` or `AllowAny`, applied to views and WebSocket consumers.
- **Filtering**: django-filter `FilterSet` classes with lookups chosen by field type, plus optional `SearchFilter` and `OrderingFilter`.
- **Signals**: `pre_save`, `post_save`, `pre_delete` and `post_delete` receivers per model.
- **WebSocket (Django Channels)**: an async CRUD consumer per model, `routing.py`, and a copy-paste setup snippet for `settings.py` and `asgi.py`.
- **API Payloads viewer**: fields table, REST requests and responses with status codes, and WebSocket messages, with copy buttons and syntax colors.
- **Live output**: everything regenerates as you type or change an option.

## Requirements

- Python 3.9 or newer
- Tkinter (included with the standard Windows and macOS installers; on Debian/Ubuntu install it with `sudo apt install python3-tk`)

## Run

```
python drf_generator.py
```

## How to use

1. Paste your Django models into the **Django models** pane (or use **Paste from clipboard** / **Load sample**).
2. Choose your options at the top. The output updates automatically.
3. Open the tabs on the right to review the generated files.
4. Use **Copy current tab** for one file, or **Save all to folder...** to write the files into your Django app folder.

### Options

| Group | Option | What it does |
|---|---|---|
| Code | Optimize queries | Adds `select_related` for ForeignKey / OneToOne and `prefetch_related` for ManyToMany |
| Code | Explicit field list | Uses `fields = ['id', ...]` instead of `'__all__'` |
| Code | urls.py | Generates the URL config |
| Permission | dropdown | Sets `permission_classes` on views and the matching checks on WebSocket consumers |
| Models import | text box | Module the models are imported from (default `.models`) |
| WebSocket | Consumers, routing, setup | Generates `consumers.py`, `routing.py` and `channels_setup.py` |
| Signals | signals.py + four checkboxes | Generates receivers for the signals you tick |
| Filtering | filters.py (django-filter) | Generates a `FilterSet` per model and wires it into the list views |
| Filtering | Search | Adds `SearchFilter` with `search_fields` taken from the text fields |
| Filtering | Ordering | Adds `OrderingFilter` with `ordering_fields` and `ordering = ['pk']` |

## Generated files

| Tab | Saved to your app folder | Notes |
|---|---|---|
| `serializers.py` | Yes | |
| `views.py` | Yes | Filter backends are set on the list views only |
| `urls.py` | Yes | |
| `filters.py` | Yes | Needs `django-filter` |
| `signals.py` | Yes | Import it in `AppConfig.ready()` (instructions are in the file header) |
| `consumers.py` | Yes | Needs `channels` |
| `routing.py` | Yes | |
| `channels_setup.py` | No | Snippet to copy into `settings.py` and `asgi.py` |
| `filtering_setup.py` | No | Snippet to copy into `settings.py` |
| `payloads.json` | Yes | Structured API docs, written by **Save all** |

## Filter lookups

| Field type | Lookups |
|---|---|
| CharField, SlugField, EmailField, URLField | `exact`, `icontains`, `istartswith` |
| TextField | `icontains` |
| Integer, Float, Decimal, DateField | `exact`, `gte`, `lte` |
| DateTimeField, TimeField | `gte`, `lte` |
| Fields with `choices` | `exact`, `in` |
| BooleanField, UUIDField, ForeignKey, OneToOneField, ManyToManyField | `exact` |
| JSONField, FileField, ImageField, DurationField | no filter |

Example request: `GET /api/registered-todas/?toda_number__icontains=12&search=juan&ordering=-registration_date`

## API Payloads tab

Pick a model, then switch between three views:

- **Fields**: type, required / read-only, and notes (max length, unique, choices, defaults).
- **REST API**: one card per endpoint (List, Create, Retrieve, Update, Partial update, Delete) with the request body and every response (`200`, `201`, `204`, `400`, `404`, and `401` / `403` for the permission you selected). The List card also lists the query parameters.
- **WebSocket**: the connect URL, then what you send, receive and what other connected clients receive for `list`, `create`, `retrieve`, `update` and `delete`, plus every error message.

The sample values are generated from the field types, so replace them with real data when you document your API.

## What it can parse

- Classes whose base name ends in `Model`, plus `AbstractUser` and `AbstractBaseUser` subclasses. Abstract models (`class Meta: abstract = True`) are skipped.
- Field definitions that are assignments (including annotated ones) calling a `...Field` or `...Key`.
- Literal keyword arguments, class-level constants, and nested `TextChoices` / `IntegerChoices`.

## Limitations

- Only top-level classes are read. Models built dynamically or imported from other files are not seen.
- Values that are not plain literals (function calls, settings lookups) are ignored when building examples.
- Generated views have no pagination. Set it in `REST_FRAMEWORK` (the setup snippet shows how).
- `ordering = ['pk']` replaces any `Meta.ordering` on the model for the generated list views.
- WebSocket `list` returns every object and does not accept filters. WebSocket `update` is always partial.
- Generated code is boilerplate. Review it before using it in production.

## Build a Windows .exe

```
pip install pyinstaller
pyinstaller --onefile --windowed --name "DRF Generator" drf_generator.py
```

The result is `dist\DRF Generator.exe`. Build on Windows to get a Windows executable.

## Project layout

```
drf_generator.py   # the whole application
README.md
```
