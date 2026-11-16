"""Publish the same query allowlists enforced by the HTTP adapters."""

from ninja import NinjaAPI

from .schemas import ErrorOut


def query_parameters(names, *, repeated=(), sortable=(), groupable=(), mandatory=(), choices=None):
    parameters = []
    for name in names:
        schema = {"type": "string"}
        if name.endswith("_id") or name in ("limit", "offset"):
            schema = {"type": "integer", "minimum": 0 if name == "offset" else 1}
            if name == "limit":
                schema.update(maximum=200, default=50)
            elif name == "offset":
                schema["default"] = 0
        elif name in ("enabled", "dry_run", "is_waste_fraction"):
            schema = {"type": "boolean"}
        elif name in ("include_totals", "include_waste", "include_quality", "history"):
            schema = {
                "type": "boolean",
                "default": name in ("include_totals", "include_waste"),
                "description": "Use the literal true or false.",
            }
        elif name in ("year", "years", "comparison_year"):
            schema = {
                "type": "integer",
                "minimum": 3 if name == "year" else 1,
                "maximum": 3 if name == "years" else 9998,
            }
            if name == "years":
                schema.update(
                    default=3, description="Calendar years ending at year; requires year."
                )
            elif name == "year":
                schema["description"] = "Focus year; cannot be combined with explicit dates."
            else:
                schema["description"] = "Baseline year, defaulting to the focus year's predecessor."
        elif name in ("active_month", "as_of") or name.endswith(("_from", "_to")):
            schema["format"] = "date-time" if name.startswith("created_") else "date"
        elif name == "sort":
            schema["description"] = "Comma-separated keys, optional - prefix: " + ", ".join(
                sortable
            )
        elif name == "group_by":
            schema["description"] = (
                "Distinct comma-separated dimensions: "
                + ", ".join(groupable)
                + ". Automatically retained: "
                + ", ".join(mandatory)
                + ". Requires grouped mode, selected implicitly if mode is omitted."
            )
        elif name == "currency":
            schema["enum"] = ["SEK"]
        elif name == "unit":
            schema["enum"] = ["kWh", "m3", "kg"]
        if choices and name in choices:
            schema["enum"] = list(choices[name])
        if name in repeated:
            schema = {
                "type": "array",
                "items": schema,
                "description": "At most 100 distinct IDs; repeat the parameter to select several.",
            }
        parameters.append(
            {
                "name": name,
                "in": "query",
                "required": False,
                "schema": schema,
                "style": "form",
                "explode": True,
            }
        )
    return parameters


def documented_query(*names, repeated=(), sortable=(), groupable=(), mandatory=(), choices=None):
    def decorate(view):
        view.api_parameters = query_parameters(
            names,
            repeated=repeated,
            sortable=sortable,
            groupable=groupable,
            mandatory=mandatory,
            choices=choices,
        )
        return view

    return decorate


class ContractAPI(NinjaAPI):
    def get_openapi_schema(self, *, path_prefix=None, path_params=None):
        schema = super().get_openapi_schema(path_prefix=path_prefix, path_params=path_params)
        error = ErrorOut.model_json_schema(ref_template="#/components/schemas/{model}")
        schema["components"]["schemas"].update(error.pop("$defs", {}))
        schema["components"]["schemas"]["ErrorOut"] = error
        by_id = {}
        for _, router in self._routers:
            for path_view in router.path_operations.values():
                for operation in path_view.operations:
                    operation_id = operation.operation_id or self.get_openapi_operation_id(
                        operation
                    )
                    by_id[operation_id] = operation.view_func
        for path, methods in schema["paths"].items():
            for method, operation in methods.items():
                view = by_id[operation["operationId"]]
                operation["parameters"].extend(getattr(view, "api_parameters", []))
                if path.endswith("/documents/{key}") and method == "delete":
                    operation["parameters"].append(
                        {
                            "name": "Deletion-Token",
                            "in": "header",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    )
                for status in (400, 401, 403, 404, 405, 409, 413, 415, 422, 503):
                    operation["responses"].setdefault(
                        str(status),
                        {
                            "description": "Structured API error",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ErrorOut"}
                                }
                            },
                        },
                    )
        return schema
