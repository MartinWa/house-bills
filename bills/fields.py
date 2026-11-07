"""Exact fixed-scale storage for SQLite; reject implicit float/string coercion."""

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.lookups import Exact


class ScaledIntegerField(models.BigIntegerField):
    def to_python(self, value):
        if value is not None and type(value) is not int:
            raise ValidationError("Use an integer in the documented minor-unit scale.")
        return value

    def get_prep_value(self, value):
        return self.to_python(value)


def integer_constraint(field, name):
    """SQLite affinity alone permits REAL values in BIGINT columns."""
    return models.CheckConstraint(
        name=name,
        condition=models.Q(**{f"{field}__isnull": True})
        | (
            Exact(
                models.Func(models.F(field), function="typeof", output_field=models.CharField()),
                models.Value("integer"),
            )
            & models.Q(**{f"{field}__gte": -(2**63), f"{field}__lte": 2**63 - 1})
        ),
    )
