
include:
- .nop
{% for role in salt['pillar.get']('available_roles') %}
{% if role not in salt['pillar.get']('roles', []) %}
- .{{ role }}
{% endif %}
{% endfor %}

