from django import template

from bills.presentation import final_day, rounded_value, scaled_value

register = template.Library()


@register.filter
def money(value):
    return scaled_value(value, 100, 2)


@register.filter
def quantity(value):
    return scaled_value(value, 1000, 3)


@register.filter
def money_whole(value):
    return rounded_value(value, 100)


@register.filter
def quantity_whole(value):
    return rounded_value(value, 1000)


register.filter("final_day", final_day)
