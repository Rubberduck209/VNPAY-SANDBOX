from django import template


register = template.Library()


@register.filter
def vnd(value):
    return '{:,}'.format(int(value)).replace(',', '.')
