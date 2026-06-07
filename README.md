# Laravel Dockerized Installer

A FastAPI service that generates ready-to-use Docker-packaged Laravel projects on demand.
Both modes run entirely on the server — the returned zip only requires `docker compose up --build` to start.

---

## Requirements

- Python ≥ 3.11
- Docker + Docker Compose
- **PHP ≥ 8.2 + Composer** — required on the API server for `/v1/new-inertia`
- **Node.js ≥ 20 + npm** — required on the API server for `/v1/new-inertia`

> When running the API via its `Dockerfile`, PHP, Composer, Node.js, and the Laravel
> installer are installed automatically inside the image.

```bash
cd api
pip install -r requirements.txt
uvicorn main:app --reload
```

Swagger UI available at `http://localhost:8000/docs`.

---

## API

### `GET /v1/release` — Docker-ready Laravel release zip

Downloads an official Laravel release from GitHub and injects Docker scaffold files into the archive.

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `version` | `string` | ✅ | — | Laravel release tag (e.g. `v11.0.0`) |
| `php_version` | `string` | | `8.4` | PHP version in the Dockerfile |
| `app_port` | `integer` | | `8080` | Host port mapped to the app container |
| `db` | `mysql` \| `postgres` \| `sqlite` | ✅ | — | Database engine |
| `app_name` | `string` | | `Laravel` | `APP_NAME` value in `.env` |

**Archive contents:**

```
laravel-<version>-docker.zip
└── laravel-<version>/
    ├── … (full Laravel source)
    ├── Dockerfile
    ├── docker-compose.yml   ← app + db + Redis
    ├── .env                 ← pre-filled, APP_KEY already set
    └── .env.docker          ← backup copy
```

**Quick start:**

```bash
unzip laravel-v11.0.0-docker.zip && cd laravel-v11.0.0
docker compose up --build
```

---

### `GET /v1/new-inertia` — Laravel + Inertia.js ready-to-run zip

Runs `laravel new` **on the API server** with the requested options and returns a complete Laravel project zip. No local PHP, Composer, or Node.js is needed on the user's machine.

#### Inertia-specific parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `starter_kit` | `react` \| `vue` \| `livewire` \| `livewire-class-components` | `react` | Inertia.js starter kit (`laravel new` flags: `--react`, `--vue`, `--livewire`, `--livewire-class-components`) |
| `auth_provider` | `laravel` \| `workos` | `laravel` | Authentication provider — Laravel built-in (Breeze) or WorkOS AuthKit |
| `teams` | `boolean` | `false` | Add multi-tenancy teams support (`--teams`) |
| `testing_framework` | `phpunit` \| `pest` | `phpunit` | Testing framework to scaffold |
| `install_boost` | `boolean` | `false` | Install `laravel/boost` for AI-assisted coding |

#### Shared Docker parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `php_version` | `string` | `8.4` | PHP version in the Dockerfile |
| `app_port` | `integer` | `8080` | Host port mapped to the Laravel app container |
| `db` | `mysql` \| `postgres` \| `sqlite` | `mysql` | Database engine |
| `app_name` | `string` | `my-app` | Application name and project directory |

**Archive contents:**

```
laravel-<app_name>-inertia-<kit>-docker.zip
└── <app_name>/
    ├── app/, config/, routes/, …  ← full Laravel project source
    ├── Dockerfile                 ← PHP CLI image for the Laravel backend
    ├── docker-compose.yml         ← app + vite (Node 20, port 5173) + db + Redis
    ├── .env                       ← pre-filled env with a generated APP_KEY
    └── .env.docker                ← backup copy of the Docker env file
```

**Quick start:**

```bash
unzip laravel-my-app-inertia-react-docker.zip
cd my-app
docker compose up --build
```

App: `http://localhost:8080`  
Vite dev server: `http://localhost:5173`

> `vendor/`, `node_modules/`, and `.git/` are excluded from the archive.
> They are installed by the containers on first start.

---

## Running the API with Docker

A `docker-compose.yml` is included at the root of this repository. The image
bundles PHP 8.2, Composer, Node.js 20, and the Laravel installer so both
endpoints can run `laravel new` / scaffold logic entirely inside the container.

```bash
docker compose up --build
```

Swagger UI: `http://localhost:8000/docs`

---

## Project structure

```
api/
├── main.py                        FastAPI application entry point
├── dto/router/v1.py               Route definitions (/v1/release, /v1/new-inertia)
├── services/
│   ├── github_service.py          Fetches Laravel release list and source zips from GitHub
│   └── installer_service.py       Builds zip archives (server-side laravel new + Jinja2 templates)
└── scaffold/
    ├── Dockerfile.j2              Dockerfile for standard releases
    ├── Dockerfile-inertia.j2      Dockerfile for Inertia projects
    ├── docker-compose.yml.j2      Compose file for standard releases
    ├── docker-compose-inertia.yml.j2  Compose file with Vite service for Inertia projects
    └── .env.docker.j2             Environment variables template
```
