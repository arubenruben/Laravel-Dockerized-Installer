import io
import zipfile
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

SCAFFOLD_DIR = Path(__file__).parent.parent / "scaffold"

# Maps each template file (relative to SCAFFOLD_DIR) to its destination
# path inside the zip (relative to the Laravel root folder).
TEMPLATES: list[tuple[str, str]] = [
    ("Dockerfile.j2", "Dockerfile"),
    ("docker-compose.yml.j2", "docker-compose.yml"),
    ("docker/nginx/default.conf.j2", "docker/nginx/default.conf"),
    (".env.docker.j2", ".env.docker"),
]

_jinja_env = Environment(
    loader=FileSystemLoader(str(SCAFFOLD_DIR)),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
)


def build_docker_zip(upstream_zip_bytes: bytes, context: dict) -> io.BytesIO:
    """
    Re-packages the upstream Laravel source zip, rendering and injecting
    Jinja2 scaffold templates from api/scaffold/:
      - Dockerfile.j2          → Dockerfile
      - docker-compose.yml.j2  → docker-compose.yml
      - docker/nginx/default.conf.j2 → docker/nginx/default.conf
      - .env.docker.j2         → .env.docker

    ``context`` is passed to every template (e.g. php_version, laravel_version,
    app_port).
    """
    output_buffer = io.BytesIO()

    with zipfile.ZipFile(io.BytesIO(upstream_zip_bytes), "r") as upstream_zip:
        with zipfile.ZipFile(output_buffer, "w", compression=zipfile.ZIP_DEFLATED) as out_zip:
            for item in upstream_zip.infolist():
                out_zip.writestr(item, upstream_zip.read(item.filename))

            root = upstream_zip.namelist()[0].split("/")[0] + "/"

            for template_path, dest_path in TEMPLATES:
                rendered = _jinja_env.get_template(template_path).render(context)
                out_zip.writestr(root + dest_path, rendered)

    output_buffer.seek(0)
    return output_buffer

