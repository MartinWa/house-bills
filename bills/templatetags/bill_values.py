from django import template

from bills.presentation import final_day, scaled_value

register = template.Library()


@register.filter
def money(value):
    return scaled_value(value, 100, 2)


@register.filter
def quantity(value):
    return scaled_value(value, 1000, 3)


register.filter("final_day", final_day)
