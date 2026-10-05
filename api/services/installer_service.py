import asyncio
import base64
import io
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

logger = logging.getLogger(__name__)

SCAFFOLD_DIR = Path(__file__).parent.parent / "scaffold"

# Maps each template file (relative to SCAFFOLD_DIR) to its destination
# path inside the zip (relative to the Laravel root folder).
TEMPLATES: list[tuple[str, str]] = [
    ("Dockerfile.j2", "Dockerfile"),
    ("docker-compose.yml.j2", "docker-compose.yml"),
    (".env.docker.j2", ".env.docker"),
    ("README.docker.md.j2", "README.md"),
]

# A template entry for the server-generated Inertia projects: (template file,
# destination path), plus an optional dict of extra context for rendering that
# one file (see ``INERTIA_SERVER_TEMPLATES_V2``).
TemplateSpec = tuple[str, str] | tuple[str, str, dict]

# Templates injected into the server-generated Inertia project (v1: dev-only,
# php-cli + php artisan serve).
INERTIA_SERVER_TEMPLATES: list[TemplateSpec] = [
    ("Dockerfile-inertia.j2", "Dockerfile"),
    ("docker-compose-inertia.yml.j2", "docker-compose.yml"),
    ("entrypoint.sh.j2", "entrypoint.sh"),
    ("README.inertia.md.j2", "README.md"),
]

# Templates injected into the server-generated Inertia project (v2: adds
# staging/production stacks running php-fpm + nginx, with dev vs. stage/prod
# entrypoints split out). Both stacks render the same deploy template; the
# extra context sets what differs: ``deploy_env`` is the value of ``APP_ENV``,
# ``env_suffix`` names the Compose project, containers and file.
INERTIA_SERVER_TEMPLATES_V2: list[TemplateSpec] = [
    ("Dockerfile-inertia-v2.j2", "Dockerfile"),
    ("docker-compose-inertia-v2.yml.j2", "docker-compose.yml"),
    (
        "docker-compose-inertia-deploy.yml.j2",
        "docker-compose.stage.yml",
        {"deploy_env": "staging", "env_suffix": "stage"},
    ),
    (
        "docker-compose-inertia-deploy.yml.j2",
        "docker-compose.prod.yml",
        {"deploy_env": "production", "env_suffix": "prod"},
    ),
    ("nginx.conf.j2", "docker/nginx.conf"),
    ("php.ini.j2", "docker/php/app.ini"),
    ("dev.entrypoint.sh.j2", "docker/dev.entrypoint.sh"),
    ("prod.entrypoint.sh.j2", "docker/prod.entrypoint.sh"),
    ("dockerignore.j2", ".dockerignore"),
    ("README.inertia-v2.md.j2", "README.md"),
]

# Composer package for each Inertia starter kit.
_STARTER_KIT_PACKAGES: dict[str, str] = {
    "react": "laravel/react-starter-kit",
    "vue": "laravel/vue-starter-kit",
    "livewire": "laravel/livewire-starter-kit",
    "livewire-class-components": "laravel/livewire-starter-kit",
}

# Valid auth feature keys accepted by `php artisan install:features --answers`
AUTH_FEATURE_KEYS: set[str] = {
    "email-verification",
    "registration",
    "2fa",
    "passkeys",
    "password-confirmation",
}

_jinja_env = Environment(
    loader=FileSystemLoader(str(SCAFFOLD_DIR)),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def generate_app_key() -> str:
    """Generate a Laravel-compatible APP_KEY (base64:… of 32 random bytes)."""
    return "base64:" + base64.b64encode(secrets.token_bytes(32)).decode()


def slugify_app_name(app_name: str) -> str:
    """
    Turn a user-supplied app name into a safe slug usable as a single
    filesystem path component (no ``..``, ``/``, or other separators).
    """
    slug = re.sub(r"[^a-z0-9-]+", "-", app_name.lower()).strip("-")
    return slug or "app"


_LINE_BREAK_RE = re.compile(r"[\r\n\x85\u2028\u2029]")


def compose_default(value: str) -> str:
    """
    Make ``value`` safe as the default of a Compose ``${VAR:-default}``.

    ``$`` would start an interpolation, so it is doubled; ``}`` would end the
    default early and Compose has no escape for it, so it is dropped; a line
    break makes Compose reject the interpolation, so it becomes a space.
    """
    return _LINE_BREAK_RE.sub(" ", value).replace("$", "$$").replace("}", "")


# Characters YAML does not allow raw in a double-quoted scalar and ``json.dumps``
# leaves alone: DEL, the C1 controls (U+0085 is a YAML line break), the Unicode
# line/paragraph separators and the BOM.
_YAML_UNPRINTABLE_RE = re.compile(r"[\x7f-\x9f\u2028\u2029\ufeff]")


def yaml_quote(value: str) -> str:
    """Render ``value`` as a YAML double-quoted scalar (a JSON string is one)."""
    return _YAML_UNPRINTABLE_RE.sub(
        lambda match: f"\\u{ord(match.group()):04x}",
        json.dumps(value, ensure_ascii=False),
    )


_jinja_env.filters.update(compose_default=compose_default, yaml_quote=yaml_quote)


def _starter_kit_ref(starter_kit: str, auth_provider: str, teams: bool) -> str:
    """
    Return the ``composer create-project`` package reference (name[:branch])
    for the requested starter kit, taking workos / teams variants into account.
    """
    package = _STARTER_KIT_PACKAGES[starter_kit]
    if starter_kit == "livewire-class-components":
        return f"{package}:dev-components"
    branch: str | None = {
        ("workos", True): "dev-workos-teams",
        ("workos", False): "dev-workos",
        ("laravel", True): "dev-teams",
    }.get((auth_provider, teams))
    return f"{package}:{branch}" if branch else package


_REMOTE_FONT_IMPORT_RE = re.compile(
    r"^\s*@import\s+url\(['\"]https?://fonts\.(?:bunny\.net|googleapis\.com)[^'\"]*['\"]\)\s*;\s*$",
    re.MULTILINE,
)


def _strip_remote_font_imports(project_dir: Path) -> None:
    """
    Remove ``@import url(...)`` lines pulling Google/Bunny fonts into the
    generated project's CSS.

    Recent ``laravel-vite-plugin`` versions self-host these fonts by
    fetching them from the network during ``npm run build`` (triggered by
    chisel's ``install:features`` apply step). The installer server has no
    route to those font CDNs, so the fetch hangs until it times out and
    fails the whole build. Stripping the import keeps the build local;
    the app simply falls back to the default Tailwind font stack.
    """
    css_dir = project_dir / "resources" / "css"
    if not css_dir.is_dir():
        return
    for css_file in css_dir.rglob("*.css"):
        original = css_file.read_text()
        stripped = _REMOTE_FONT_IMPORT_RE.sub("", original)
        if stripped != original:
            css_file.write_text(stripped)


_VITE_DEFINE_CONFIG_RE = re.compile(r"defineConfig\(\{")

_VITE_DEV_SERVER_CONFIG = """server: {
        host: '0.0.0.0',
        hmr: {
            host: 'localhost',
        },
    },
"""


def _configure_vite_dev_server(project_dir: Path) -> None:
    """
    Add an explicit ``server`` block to the generated ``vite.config.ts``.

    The dev container runs ``vite --host``, which binds to all interfaces
    (``::``) inside the container. Without an explicit ``server.hmr.host``,
    laravel-vite-plugin falls back to that raw bind address when writing
    ``public/hot``, producing an URL like ``http://[::]:5173`` that's
    unreachable from the host browser. Laravel then can't detect the dev
    server and falls back to the (nonexistent) production manifest,
    raising ``ViteManifestNotFoundException``. Pinning ``host: '0.0.0.0'``
    and ``hmr.host: 'localhost'`` keeps the container listening everywhere
    while reporting the host-reachable address (via the ``5173:5173`` port
    mapping) for assets/HMR.
    """
    vite_config = project_dir / "vite.config.ts"
    if not vite_config.is_file():
        return
    original = vite_config.read_text()
    if re.search(r"\bserver\s*:", original):
        return
    patched, count = _VITE_DEFINE_CONFIG_RE.subn(
        "defineConfig({\n    " + _VITE_DEV_SERVER_CONFIG, original, count=1
    )
    if count:
        vite_config.write_text(patched)


_WITH_MIDDLEWARE_RE = re.compile(
    r"(->withMiddleware\(function \(Middleware \$middleware\)(?:: void)?\s*\{\n)"
)

_TRUST_PROXIES_SNIPPET = """        // The app container is only ever reached through the Docker
        // reverse proxy (see docker-compose.*.yml), which terminates TLS
        // and forwards plain HTTP with X-Forwarded-* headers. Without
        // trusting it, Laravel sees every request as HTTP and generates
        // insecure (http://) URLs for assets, redirects, etc., and
        // $request->ip() resolves to the proxy instead of the real client.
        $middleware->trustProxies(
            at: '*',
            headers: SymfonyRequest::HEADER_X_FORWARDED_FOR
                | SymfonyRequest::HEADER_X_FORWARDED_HOST
                | SymfonyRequest::HEADER_X_FORWARDED_PORT
                | SymfonyRequest::HEADER_X_FORWARDED_PROTO,
        );

"""

_LAST_USE_STATEMENT_RE = re.compile(r"(^use [^\n]+;\n)(?!use )", re.MULTILINE)


def _configure_trusted_proxies(project_dir: Path) -> None:
    """
    Trust the Docker reverse proxy's forwarded headers in ``bootstrap/app.php``.

    The generated app container is only reachable through an external
    TLS-terminating reverse proxy on the Docker ``proxy-net`` network, which
    forwards plain HTTP. Without ``trustProxies``, ``Request::isSecure()``
    always evaluates to false (causing Vite/route URLs to be generated as
    ``http://`` and get blocked as mixed content once loaded over HTTPS),
    and ``$request->ip()`` resolves to the proxy rather than the real
    client, silently breaking per-IP rate limiting (e.g. Fortify's login
    throttle).
    """
    app_php = project_dir / "bootstrap" / "app.php"
    if not app_php.is_file():
        return
    original = app_php.read_text()
    if "trustProxies" in original:
        return
    patched, count = _WITH_MIDDLEWARE_RE.subn(
        r"\1" + _TRUST_PROXIES_SNIPPET, original, count=1
    )
    if not count:
        return
    if "Symfony\\Component\\HttpFoundation\\Request as SymfonyRequest" not in patched:
        patched, import_count = _LAST_USE_STATEMENT_RE.subn(
            r"\1use Symfony\\Component\\HttpFoundation\\Request as SymfonyRequest;\n",
            patched,
            count=1,
        )
        if not import_count:
            return
    app_php.write_text(patched)


_PRELOAD_LINK_HEADERS_ENTRY_RE = re.compile(
    r"^[ \t]*AddLinkHeadersForPreloadedAssets::class,[ \t]*\n", re.MULTILINE
)

_PRELOAD_LINK_HEADERS_IMPORT_RE = re.compile(
    r"^use Illuminate\\Http\\Middleware\\AddLinkHeadersForPreloadedAssets;\n",
    re.MULTILINE,
)

_WEB_MIDDLEWARE_APPEND_RE = re.compile(
    r"^([ \t]*)\$middleware->web\(append: \[", re.MULTILINE
)

_PRELOAD_LINK_HEADERS_COMMENT = (
    "// Deliberately not registering AddLinkHeadersForPreloadedAssets: it",
    '// duplicates the <link rel="preload"> tags @vite()/@fonts already emit',
    "// in the document <head>, and its only extra effect, enabling HTTP/2",
    "// Server Push, is a feature no major browser still honors. With enough",
    "// fonts/chunks, the resulting Link header can exceed reverse-proxy",
    "// header-buffer limits and cause 502s.",
)


def _remove_preload_link_headers(project_dir: Path) -> None:
    """
    Drop ``AddLinkHeadersForPreloadedAssets`` from ``bootstrap/app.php``.

    The starter kits append this middleware to the ``web`` group. It copies
    every asset Vite preloads (fonts, JS chunks, CSS) into a ``Link``
    response header, which grows with every font weight and chunk until it
    exceeds the reverse proxy's header-buffer limit and the request fails
    with a 502. It only duplicates the ``<link rel="preload">`` tags that
    ``@vite()`` already emits in the document ``<head>``; its one extra
    effect, HTTP/2 Server Push, is no longer honored by any major browser.
    Goes with ``_configure_trusted_proxies``, since both exist because the
    app always sits behind a reverse proxy.

    Does nothing if the middleware isn't registered, so it is idempotent
    and safe if a starter kit stops shipping it.
    """
    app_php = project_dir / "bootstrap" / "app.php"
    if not app_php.is_file():
        return
    original = app_php.read_text()
    patched, count = _PRELOAD_LINK_HEADERS_ENTRY_RE.subn("", original)
    if not count:
        return
    patched = _PRELOAD_LINK_HEADERS_IMPORT_RE.sub("", patched)
    patched = _WEB_MIDDLEWARE_APPEND_RE.sub(
        lambda match: "".join(
            f"{match.group(1)}{line}\n" for line in _PRELOAD_LINK_HEADERS_COMMENT
        )
        + match.group(0),
        patched,
        count=1,
    )
    app_php.write_text(patched)


_BOOT_METHOD_RE = re.compile(r"(public function boot\(\): void\s*\{\n)")

_FORCE_SCHEME_SNIPPET = """        // The app container sits behind a reverse proxy that terminates TLS
        // and forwards plain HTTP, so Laravel sees every request as
        // insecure. Force the scheme from APP_URL rather than trusting
        // proxy headers, so generated asset/route URLs stay HTTPS even if
        // the proxy ever fails to forward X-Forwarded-Proto correctly.
        if (str_starts_with(config('app.url'), 'https://')) {
            URL::forceScheme('https');
        }

"""


def _configure_force_https_scheme(project_dir: Path) -> None:
    """
    Force the URL scheme from ``APP_URL`` in ``AppServiceProvider::boot()``.

    Acts as a fallback independent of proxy headers, alongside
    ``_configure_trusted_proxies``: if the reverse proxy ever fails to
    forward ``X-Forwarded-Proto``, generated URLs still stay ``https://``
    whenever ``APP_URL`` itself is HTTPS.
    """
    provider_php = project_dir / "app" / "Providers" / "AppServiceProvider.php"
    if not provider_php.is_file():
        return
    original = provider_php.read_text()
    if "forceScheme" in original:
        return
    patched, count = _BOOT_METHOD_RE.subn(
        r"\1" + _FORCE_SCHEME_SNIPPET, original, count=1
    )
    if not count:
        return
    if "Illuminate\\Support\\Facades\\URL" not in patched:
        patched, import_count = _LAST_USE_STATEMENT_RE.subn(
            r"\1use Illuminate\\Support\\Facades\\URL;\n", patched, count=1
        )
        if not import_count:
            return
    provider_php.write_text(patched)


# Matches the entry whether ``telescope:install`` wrote it fully qualified
# (``App\Providers\TelescopeServiceProvider::class,``) or a starter kit
# imports the class and lists it bare.
_TELESCOPE_PROVIDER_ENTRY_RE = re.compile(
    r"^[ \t]*(?:\\?App\\Providers\\)?TelescopeServiceProvider::class,?[ \t]*\n",
    re.MULTILINE,
)

_TELESCOPE_PROVIDER_IMPORT_RE = re.compile(
    r"^use App\\Providers\\TelescopeServiceProvider;\n", re.MULTILINE
)

_REGISTER_METHOD_RE = re.compile(
    r"(public function register\(\)(?:: void)?\s*\{\n)([ \t]*//[ \t]*\n)?"
)

_TELESCOPE_REGISTER_SNIPPET = """        // Telescope is a require-dev package (never installed in the
        // `composer install --no-dev` production/CI image), so it's only
        // safe to register when its base class actually exists.
        if (class_exists(TelescopeApplicationServiceProvider::class)) {
            $this->app->register(TelescopeServiceProvider::class);
        }
"""

_TELESCOPE_BASE_CLASS_IMPORT = "use Laravel\\Telescope\\TelescopeApplicationServiceProvider;\n"


def _configure_telescope_dev_only(project_dir: Path) -> None:
    """
    Register ``App\\Providers\\TelescopeServiceProvider`` only when Telescope is installed.

    ``telescope:install`` lists that provider in ``bootstrap/providers.php``,
    and it extends ``Laravel\\Telescope\\TelescopeApplicationServiceProvider``.
    Telescope is a ``require-dev`` package and the generated image runs
    ``composer install --no-dev``, so in staging and production that base class
    is missing and every request would fatal. Move the registration into
    ``AppServiceProvider::register()`` behind a ``class_exists`` check instead.

    Unlike the other patches, this one raises when it cannot apply: leaving
    the provider registered unconditionally would ship a project that works
    in dev and fatals the moment the production stack boots.
    """
    providers_php = project_dir / "bootstrap" / "providers.php"
    provider_php = project_dir / "app" / "Providers" / "AppServiceProvider.php"

    if providers_php.is_file():
        providers = providers_php.read_text()
        providers = _TELESCOPE_PROVIDER_ENTRY_RE.sub("", providers)
        providers = _TELESCOPE_PROVIDER_IMPORT_RE.sub("", providers)
        if "TelescopeServiceProvider" in providers:
            raise RuntimeError(
                "bootstrap/providers.php still references TelescopeServiceProvider "
                "after removing its entry; it would fatal under `composer install --no-dev`."
            )
        providers_php.write_text(providers)

    if not provider_php.is_file():
        raise RuntimeError("app/Providers/AppServiceProvider.php not found; cannot register Telescope.")
    original = provider_php.read_text()
    if "TelescopeApplicationServiceProvider" in original:
        return
    patched, count = _REGISTER_METHOD_RE.subn(
        lambda match: match.group(1) + _TELESCOPE_REGISTER_SNIPPET, original, count=1
    )
    if not count:
        raise RuntimeError(
            "Could not find `public function register()` in AppServiceProvider.php "
            "to register Telescope in."
        )
    patched, import_count = _LAST_USE_STATEMENT_RE.subn(
        lambda match: match.group(1) + _TELESCOPE_BASE_CLASS_IMPORT, patched, count=1
    )
    if not import_count:
        raise RuntimeError("Could not find a `use` statement in AppServiceProvider.php to extend.")
    provider_php.write_text(patched)


def _build_env() -> dict[str, str]:
    """Return an os.environ copy augmented with the Composer global bin dir."""
    env = os.environ.copy()
    env.update({"TERM": "dumb", "NO_COLOR": "1"})
    try:
        result = subprocess.run(
            ["composer", "global", "config", "bin-dir", "--absolute"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.stdout.strip():
            env["PATH"] = f"{result.stdout.strip()}:{env.get('PATH', '')}"
    except Exception as exc:
        logger.warning("Could not determine Composer global bin-dir: %s", exc)
    return env


def _run(cmd: list[str], *, cwd: Path | str, env: dict, timeout: int, check: bool = True) -> None:
    subprocess.run(
        cmd,
        cwd=str(cwd),
        check=check,
        env=env,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_docker_zip(upstream_zip_bytes: bytes, context: dict) -> io.BytesIO:
    """
    Re-packages the upstream Laravel source zip, generating an APP_KEY server-side
    and rendering/injecting Docker scaffold files so the archive is ready to run with
    ``docker compose up --build`` — no manual steps required.

    Injected files:
      - Dockerfile
      - docker-compose.yml
      - .env         ← pre-filled, includes a generated APP_KEY
      - .env.docker  ← backup copy
      - README.md
    """
    app_key = generate_app_key()
    ctx = {
        **context,
        "app_key": app_key,
        "app_slug": slugify_app_name(context["app_name"]),
    }

    output_buffer = io.BytesIO()

    with zipfile.ZipFile(io.BytesIO(upstream_zip_bytes), "r") as upstream_zip:
        with zipfile.ZipFile(output_buffer, "w", compression=zipfile.ZIP_DEFLATED) as out_zip:
            for item in upstream_zip.infolist():
                out_zip.writestr(item, upstream_zip.read(item.filename))

            root = upstream_zip.namelist()[0].split("/")[0] + "/"

            for template_path, dest_path in TEMPLATES:
                rendered = _jinja_env.get_template(template_path).render(ctx)
                out_zip.writestr(root + dest_path, rendered)

            # Inject .env (ready to use — no cp step needed) alongside .env.docker
            env_content = _jinja_env.get_template(".env.docker.j2").render(ctx)
            out_zip.writestr(root + ".env", env_content)

    output_buffer.seek(0)
    return output_buffer


async def build_inertia_project_zip(
    context: dict, templates: list[TemplateSpec] = INERTIA_SERVER_TEMPLATES
) -> io.BytesIO:
    """
    Scaffolds a complete Laravel + Inertia.js project on the server and returns
    a Docker-ready zip. See ``_build_inertia_project_zip_sync`` for the full flow.
    """
    return await asyncio.to_thread(_build_inertia_project_zip_sync, context, templates)


# ---------------------------------------------------------------------------
# Private — synchronous implementation
# ---------------------------------------------------------------------------


def _build_inertia_project_zip_sync(
    context: dict, templates: list[TemplateSpec] = INERTIA_SERVER_TEMPLATES
) -> io.BytesIO:
    """
    Server-side project generation flow:

    1. ``composer create-project <kit> <name> --no-scripts`` — installs PHP
       deps without running any post-install artisan commands or migrations.
    2. Copy ``.env.example`` → ``.env``, run ``package:discover``,
       ``key:generate``.
    3. If ``testing_framework == "pest"``, swap PHPUnit for Pest
       (``composer remove phpunit/phpunit``, ``composer require pestphp/pest``,
       ``./vendor/bin/pest --init``). Every Composer call before step 5 runs
       with ``--no-scripts`` — see the note in step 3.
       If ``queue == "redis"``, ``composer require predis/predis`` (same rule),
       then ``package:discover``.
       If ``telescope`` is set, ``composer require laravel/telescope --dev``
       (same rule), ``package:discover``, ``telescope:install``, then
       ``_configure_telescope_dev_only`` so the ``--no-dev`` image still boots.
       If ``horizon`` is set, ``composer require laravel/horizon`` (same rule),
       ``package:discover`` and ``horizon:install``.
    4. ``npm install`` — required before ``install:features`` because chisel's
       ``apply`` callback runs ``npm run lint`` / ``npm run format``.
    5. ``php artisan install:features --no-interaction --answers=<json>`` —
       sculpts the project according to the requested auth features (default: none).
       For Pest, the remaining PHPUnit tests are then converted with
       ``pest-plugin-drift`` (after chisel, which prunes tests by PHPUnit form).
    6. Read the generated APP_KEY; render and write Docker scaffold files.
    7. Overwrite ``.env`` with the Docker-ready environment (DB → Docker service
       hostnames, Redis, etc.).
    8. Zip everything except ``vendor/``, ``node_modules/``, ``.git/``,
       and ``public/build/`` (Vite handles assets at runtime).
    """
    app_name = slugify_app_name(context["app_name"])
    env = _build_env()

    # Optional: install laravel/boost globally
    if context.get("install_boost"):
        _run(
            ["composer", "global", "require", "laravel/boost", "--dev", "--no-interaction", "-q"],
            cwd="/tmp",
            env=env,
            timeout=180,
        )

    package_ref = _starter_kit_ref(
        context["starter_kit"], context["auth_provider"], context["teams"]
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        project_dir = Path(tmp_dir) / app_name

        # ── 1. Create project (skip scripts so we control the full flow) ─────
        _run(
            [
                "composer", "create-project", package_ref, app_name,
                "--stability=dev", "--no-interaction", "--no-scripts",
            ],
            cwd=tmp_dir,
            env=env,
            timeout=600,
        )

        # ── 2. Bootstrap Laravel ─────────────────────────────────────────────
        if not (project_dir / ".env").exists():
            shutil.copy(project_dir / ".env.example", project_dir / ".env")

        # Discover packages (replaces post-autoload-dump hook we skipped)
        _run(
            ["php", "artisan", "package:discover", "--ansi"],
            cwd=project_dir,
            env=env,
            timeout=60,
            check=False,
        )
        _run(
            ["php", "artisan", "key:generate", "--ansi"],
            cwd=project_dir,
            env=env,
            timeout=60,
        )

        # ── 3. Swap the test runner to Pest, if requested ─────────────────────
        # Every ``composer remove`` / ``composer require`` that runs before
        # step 5 MUST pass ``--no-scripts``. The starter kits ship
        # ``post-update-cmd: ["@php artisan install:features", ...]`` until
        # chisel has run, and both commands fire ``post-update-cmd``. Letting it
        # run would execute ``install:features`` with no answers, and chisel
        # deletes itself afterwards, so the real step 5 would fail with
        # "Command install:features is not defined". Any future option that
        # adds a package before step 5 should follow the same pattern: pass
        # ``--no-scripts`` and rely on the ``package:discover`` below.
        if context.get("testing_framework") == "pest":
            # Same flow as ``laravel new --pest`` (laravel/installer NewCommand).
            _run(
                ["composer", "remove", "phpunit/phpunit", "--dev", "--no-interaction", "--no-scripts"],
                cwd=project_dir,
                env=env,
                timeout=120,
            )
            _run(
                [
                    "composer", "require", "pestphp/pest", "pestphp/pest-plugin-laravel",
                    "--dev", "--no-interaction", "-W", "--no-scripts",
                ],
                cwd=project_dir,
                env=env,
                timeout=180,
            )
            # Pest 2+ has no ``pest:install`` artisan command (that was Pest 1's
            # Laravel plugin); ``--init`` scaffolds tests/Pest.php instead.
            pest_env = {**env, "PEST_NO_SUPPORT": "true"}
            _run(
                ["php", "./vendor/bin/pest", "--init"],
                cwd=project_dir,
                env=pest_env,
                timeout=60,
            )

            # ``--no-scripts`` skipped the hooks' package discovery; this is the
            # part of them the installer still needs.
            _run(
                ["php", "artisan", "package:discover", "--ansi"],
                cwd=project_dir,
                env=env,
                timeout=60,
            )

        # ── 3b. Redis queue client ────────────────────────────────────────────
        # predis is pure PHP, so the image needs no ``phpredis`` extension. Same
        # ``--no-scripts`` rule as step 3.
        if context.get("queue") == "redis":
            _run(
                ["composer", "require", "predis/predis", "--no-interaction", "--no-scripts"],
                cwd=project_dir,
                env=env,
                timeout=180,
            )
            _run(
                ["php", "artisan", "package:discover", "--ansi"],
                cwd=project_dir,
                env=env,
                timeout=60,
            )

        # ── 3c. Telescope (dev only) ──────────────────────────────────────────
        # ``--dev`` keeps it out of the ``composer install --no-dev`` image, and
        # ``_configure_telescope_dev_only`` makes sure nothing in the app needs
        # it there. Same ``--no-scripts`` rule as step 3.
        if context.get("telescope"):
            _run(
                ["composer", "require", "laravel/telescope", "--dev", "--no-interaction", "--no-scripts"],
                cwd=project_dir,
                env=env,
                timeout=180,
            )
            _run(
                ["php", "artisan", "package:discover", "--ansi"],
                cwd=project_dir,
                env=env,
                timeout=60,
            )
            _run(
                ["php", "artisan", "telescope:install", "--no-interaction"],
                cwd=project_dir,
                env=env,
                timeout=60,
            )
            _configure_telescope_dev_only(project_dir)

        # ── 3d. Laravel Horizon ───────────────────────────────────────────────
        # Same ``--no-scripts`` rule as step 3. The image has ``pcntl`` and
        # ``posix``, which Horizon requires, but this host may not; Composer
        # would refuse the install here, so those platform checks are skipped.
        # ``horizon:install`` publishes ``config/horizon.php`` and
        # ``HorizonServiceProvider`` and registers the provider in
        # ``bootstrap/providers.php``; it needs the package discovered first.
        if context.get("horizon"):
            _run(
                [
                    "composer", "require", "laravel/horizon", "--no-interaction", "--no-scripts",
                    "--ignore-platform-req=ext-pcntl", "--ignore-platform-req=ext-posix",
                ],
                cwd=project_dir,
                env=env,
                timeout=180,
            )
            _run(
                ["php", "artisan", "package:discover", "--ansi"],
                cwd=project_dir,
                env=env,
                timeout=60,
            )
            _run(
                ["php", "artisan", "horizon:install", "--no-interaction"],
                cwd=project_dir,
                env=env,
                timeout=60,
            )

        # ── 4. npm install (chisel apply callback needs node_modules) ─────────
        # Strip remote Google/Bunny font imports first: laravel-vite-plugin
        # self-hosts these by fetching them at build time, and this server
        # has no network route to those font CDNs (see _strip_remote_font_imports).
        _strip_remote_font_imports(project_dir)
        _configure_vite_dev_server(project_dir)
        _configure_trusted_proxies(project_dir)
        _remove_preload_link_headers(project_dir)
        _configure_force_https_scheme(project_dir)
        _run(["npm", "install"], cwd=project_dir, env=env, timeout=300)

        # ── 5. Sculpt auth features via chisel ───────────────────────────────
        # Default: no features. Pass explicit list to select specific ones.
        auth_features: list[str] = [
            f for f in context.get("auth_features", []) if f in AUTH_FEATURE_KEYS
        ]
        answers_json = json.dumps({"auth_features": auth_features})
        _run(
            ["php", "artisan", "install:features", "--no-interaction", f"--answers={answers_json}"],
            cwd=project_dir,
            env=env,
            timeout=300,
        )

        # ── 5b. Convert the kit's PHPUnit tests to Pest syntax ────────────────
        # Deliberately after chisel, as in ``laravel new --pest``: chisel prunes
        # the tests of unselected features by their PHPUnit method form, so it
        # cannot see them once they are Pest closures (a ``2fa`` project without
        # ``password-confirmation`` would keep a test that can only fail). Still
        # ``--no-scripts``, consistent with the rule in step 3.
        if context.get("testing_framework") == "pest":
            pest_env = {**env, "PEST_NO_SUPPORT": "true"}
            _run(
                ["composer", "require", "pestphp/pest-plugin-drift", "--dev", "--no-interaction", "--no-scripts"],
                cwd=project_dir,
                env=env,
                timeout=180,
            )
            _run(
                ["php", "./vendor/bin/pest", "--drift"],
                cwd=project_dir,
                env=pest_env,
                timeout=120,
            )
            # One-shot conversion plugin: drop it again.
            _run(
                ["composer", "remove", "pestphp/pest-plugin-drift", "--dev", "--no-interaction", "--no-scripts"],
                cwd=project_dir,
                env=env,
                timeout=120,
            )

        # ── 6. Read APP_KEY ───────────────────────────────────────────────────
        app_key = ""
        env_file = project_dir / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("APP_KEY="):
                    app_key = line.split("=", 1)[1].strip()
                    break

        # ── 7. Write Docker scaffold files ────────────────────────────────────
        ctx = {**context, "app_key": app_key, "app_slug": app_name}
        for template_path, dest_path, *extra in templates:
            rendered = _jinja_env.get_template(template_path).render({**ctx, **(extra[0] if extra else {})})
            dest = project_dir / dest_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(rendered)
            if dest_path.endswith(".sh"):
                dest.chmod(0o755)

        env_docker = _jinja_env.get_template(".env.docker.j2").render(ctx)
        (project_dir / ".env.docker").write_text(env_docker)
        (project_dir / ".env").write_text(env_docker)  # ready for docker compose

        # ── 8. Zip the project ────────────────────────────────────────────────
        _EXCLUDE_TOPS = {"vendor", "node_modules", ".git"}

        output_buffer = io.BytesIO()
        with zipfile.ZipFile(output_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for file_path in sorted(project_dir.rglob("*")):
                if not file_path.is_file():
                    continue
                rel = file_path.relative_to(project_dir)
                if rel.parts[0] in _EXCLUDE_TOPS:
                    continue
                # Skip Vite build artifacts — the dev container handles assets
                if len(rel.parts) >= 2 and rel.parts[:2] == ("public", "build"):
                    continue
                zf.write(file_path, f"{app_name}/{rel}")

        output_buffer.seek(0)
        return output_buffer

