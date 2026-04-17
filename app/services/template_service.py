from jinja2 import Environment, FileSystemLoader
import os
import json

TEMPLATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "templates"
)

env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=False,
    extensions=["jinja2.ext.do"]  # REQUIRED for {% do %}
)


def render_template(template_name: str, input_data: dict) -> str:
    template = env.get_template(template_name)

    # Pass input JSON as GeneralMap
    return template.render(GeneralMap=input_data)


def render_json_template(input_data: dict) -> dict:
    rendered = render_template("json_fairsharing.j2", input_data)
    return json.loads(rendered)


def render_turtle_template(input_data: dict) -> str:
    return render_template("turtle_fairsharing.j2", input_data)