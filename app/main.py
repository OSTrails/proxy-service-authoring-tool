from fastapi import FastAPI, Body
from fastapi.responses import JSONResponse, PlainTextResponse
from services.template_service import (
    render_json_template,
    render_turtle_template
)

import httpx, uvicorn, os, base64, re, traceback
from fastapi import HTTPException, FastAPI, Request
from fastapi.responses import JSONResponse
from rdflib import Graph, URIRef, Literal, Namespace
from urllib.parse import urlparse
from rdflib.namespace import DCTERMS


app = FastAPI(
    title="OSTrails proxy service",
    description="Proxy for processing and submitting RDF metadata records to GitHub and FAIRsharing.",
    version="1.2.1",
    docs_url="/questionnaire/docs",
    redoc_url=None,
    openapi_url="/questionnaire/openapi.json",
)

@app.get("/questionnaire/", summary="Health check", description="Verify that the API is running correctly.")
async def health_check():
    return {
        "status": "ok",
        "message": "API is running. See /questionnaire/docs for interactive documentation.",
    }
    
@app.head(
    "/questionnaire/", summary="Health check (HEAD)", description="HEAD health check. Returns headers only if the API is running.",)
async def health_check_head():
    return

# ═══════════════════════════════════════════════════════════════════
# Environment configuration
# ═══════════════════════════════════════════════════════════════════


AUTH_URL = os.getenv("AUTH_URL")
DATA_URL = os.getenv("DATA_URL")
USERNAME = os.getenv("USERNAME")
PASSWORD = os.getenv("PASSWORD")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_OWNER = "OSTrails"
GITHUB_REPO = "assessment-component-metadata-records"
GITHUB_BRANCH = "main"

print(AUTH_URL)
print(DATA_URL)
print(USERNAME)

# FAIRsharing GraphQL settings
FAIRSHARING_GRAPHQL_ENDPOINT = "https://api.fairsharing.org/graphql"
FAIRSHARING_GRAPHQL_KEY = "484de7ca-4496-4ee7-8cbf-578d2923c08f"

DCTERMS = Namespace("http://purl.org/dc/terms/")

# ═══════════════════════════════════════════════════════════════════
############################### GITHUB ##############################
# ═══════════════════════════════════════════════════════════════════

# ─────────────────────────────────────────────────────────────
# Render Turtle using Jinja2
# ─────────────────────────────────────────────────────────────

async def render_json(input_json: dict = Body(...)):
    """
    Render FAIRsharing-compatible JSON
    """
    rendered = render_json_template(input_json)
    return JSONResponse(content=rendered)

# ─────────────────────────────────────────────────────────────
# Helper functions
# ─────────────────────────────────────────────────────────────
def _extract_record_info(rdf_text: str):
    """Extract record_id, category, and URI from RDF Turtle content."""
    g = Graph()
    try:
        g.parse(data=rdf_text, format="turtle")
    except Exception as e:
        raise HTTPException(400, f"Invalid RDF format: {e}")

    uri_candidate = None
    for s, _, _ in g.triples((None, DCTERMS.identifier, None)):
        if isinstance(s, URIRef):
            uri_candidate = str(s)
            break

    if uri_candidate is None:
        raise HTTPException(400, "No valid identifier or subject URI found in RDF.")

    path_parts = [p for p in urlparse(uri_candidate).path.split("/") if p]
    if len(path_parts) < 2:
        raise HTTPException(400, f"URI '{uri_candidate}' is malformed or missing path structure.")

    filename = path_parts[-1]
    category = path_parts[-2]
    record_id = re.sub(r"\.ttl$", "", filename, flags=re.IGNORECASE)

    return record_id, category, uri_candidate


# ─────────────────────────────────────────────────────────────
# GitHub Commit Function
# ─────────────────────────────────────────────────────────────
async def commit_rdf_to_github(client: httpx.AsyncClient, rdf_text: str):
    """Commit or update an RDF Turtle record into the GitHub repository."""
    if not all([GITHUB_TOKEN, GITHUB_OWNER, GITHUB_REPO]):
        raise HTTPException(500, "GitHub credentials or configuration missing.")

    record_id, category, uri_candidate = _extract_record_info(rdf_text)
    path = f"{category.rstrip('/')}/{record_id}.ttl"
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    try:
        sha = None
        pre = await client.get(url, headers=headers)
        if pre.status_code == 200:
            sha = pre.json().get("sha")
        elif pre.status_code not in (404,):
            raise HTTPException(500, f"GitHub preflight failed: {pre.text}")

        payload = {
            "message": f"Add or update RDF record '{record_id}' in category '{category}'.",
            "content": base64.b64encode(rdf_text.encode()).decode(),
            "branch": GITHUB_BRANCH,
            **({"sha": sha} if sha else {}),
        }

        put = await client.put(url, headers=headers, json=payload)
        put.raise_for_status()

        commit_data = put.json()
        return {
            "status": "success",
            "action": "update" if sha else "create",
            "record_id": record_id,
            "category": category,
            "commit_url": commit_data.get("commit", {}).get("html_url"),
            "file_url": commit_data.get("content", {}).get("html_url"),
            "message": (
                f"RDF record '{record_id}' successfully {'updated' if sha else 'created'} "
                f"in GitHub repository '{GITHUB_REPO}'."
            ),
        }

    except httpx.HTTPError as e:
        raise HTTPException(500, f"GitHub request failed: {str(e)}")
    except Exception as e:
        raise HTTPException(500, f"Unexpected error during GitHub commit: {e}")
    
# ─────────────────────────────────────────────────────────────
# GitHub Push Endpoint
# ─────────────────────────────────────────────────────────────
@app.post(
    "/questionnaire/push",
    summary="Push RDF record to GitHub",
    description="""
    Upload an RDF record (in Turtle format) to the OSTrails GitHub repository.
    Automatically creates or updates the corresponding `.ttl` file under its category folder.
    Returns structured feedback including commit URLs and record identifiers.
    """,
)
async def githubpush(input_json: dict = Body(...)):
    try:
        # Step 1 — Render RDF
        rdf_text = render_turtle_template(input_json)

        # Step 2 — Commit to GitHub        
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await commit_rdf_to_github(client, rdf_text)
            return JSONResponse(content=response, status_code=200)

    except HTTPException as e:
        return JSONResponse(
            status_code=e.status_code,
            content={"status": "error", "message": e.detail},
        )
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "message": f"Unexpected internal error: {e}",
                "trace": traceback.format_exc().splitlines()[-5:],
            },
        )


# ═══════════════════════════════════════════════════════════════════
########################## FAIRsharing ##############################
# ═══════════════════════════════════════════════════════════════════

# ─────────────────────────────────────────────────────────────
# Render JSON using Jinja2
# ─────────────────────────────────────────────────────────────

async def render_json(input_json: dict = Body(...)):
    """
    Render FAIRsharing-compatible JSON
    """
    rendered = render_json_template(input_json)
    return JSONResponse(content=rendered)

# ─────────────────────────────────────────────────────────────
# Helper: resolve subject/domain IDs (inside fairsharing_record)
# ─────────────────────────────────────────────────────────────
async def fetch_internal_id(client: httpx.AsyncClient, iri: str, type_: str):
    if type_ == "subject":
        query_field = "searchSubjects"
    elif type_ == "domain":
        query_field = "searchDomains"
    else:
        raise ValueError("Unknown type_")

    query = {
        "query": f"""
        query {{
            {query_field}(q: "{iri}") {{
            id
            iri
            }}
        }}
        """
    }

    try:
        resp = await client.post(
            FAIRSHARING_GRAPHQL_ENDPOINT,
            json=query,
            headers={"x-graphql-key": FAIRSHARING_GRAPHQL_KEY},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("data", {}).get(query_field, [])
        if results and isinstance(results, list) and results[0].get("id"):
            return results[0]["id"]
    except Exception as e:
        print(f"GraphQL query failed for {iri}: {e}")
    return None


async def resolve_subject_domain_ids(body_dict: dict) -> dict:
    async with httpx.AsyncClient() as client:
        record = body_dict.get("fairsharing_record", {})

        # Resolve subjects
        if "subject_ids" in record and isinstance(record["subject_ids"], list):
            resolved_subjects = []
            for iri in record["subject_ids"]:
                internal_id = await fetch_internal_id(client, iri, "subject")
                if internal_id is not None:
                    resolved_subjects.append(internal_id)
                else:
                    print(f"Removed subject URI without internal ID: {iri}")
            record["subject_ids"] = resolved_subjects

        # Resolve domains
        if "domain_ids" in record and isinstance(record["domain_ids"], list):
            resolved_domains = []
            for iri in record["domain_ids"]:
                internal_id = await fetch_internal_id(client, iri, "domain")
                if internal_id is not None:
                    resolved_domains.append(internal_id)
                else:
                    print(f"Removed domain URI without internal ID: {iri}")
            record["domain_ids"] = resolved_domains

        body_dict["fairsharing_record"] = record
    return body_dict
# ─────────────────────────────────────────────────────────────
# Remove empty JSON keys, create an error in FAIRsharing
# ─────────────────────────────────────────────────────────────
def remove_empty(obj):
    if isinstance(obj, dict):
        
        return {
            k: remove_empty(v)
            for k, v in obj.items()
            if v not in (None, "", [], {}) and remove_empty(v) != {}
        }
    elif isinstance(obj, list):
        cleaned = [remove_empty(v) for v in obj if v not in (None, "", [], {})]
        return [v for v in cleaned if v != {}]
    else:
        return obj

# ═══════════════════════════════════════════════════════════════════
# Submit FAIRsharing endpoint
# ═══════════════════════════════════════════════════════════════════
@app.post("/questionnaire/submit-onlyFAIRsharing", summary="Submit record to FAIRsharing",response_class=JSONResponse)
async def submit_record(input_json: dict = Body(...)):
    """Authenticate with FAIRsharing, resolve subject/domain IDs, and submit the cleaned record."""

    body_dict = render_json_template(input_json)

    body_dict = await resolve_subject_domain_ids(body_dict)

    body_dict = remove_empty(body_dict)
    # print(json.dumps(body_dict, indent=2))  # Double-quoted JSON for readability
    
    async with httpx.AsyncClient() as client:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        auth = await client.post(
            AUTH_URL,
            headers=headers,
            json={"user": {"login": USERNAME, "password": PASSWORD}},
            timeout=15.0,
        )
        auth.raise_for_status()
        token = auth.json().get("jwt")
        if not token:
            raise HTTPException(500, "Missing jwt token")

        headers["Authorization"] = f"Bearer {token}"
        data_response = await client.post(DATA_URL, json=body_dict, headers=headers)
        data_response.raise_for_status()

        return {
            "status": "success",
            "data_status_code": data_response.status_code,
            "response": data_response.json(),
        }


@app.post("/questionnaire/submit", summary="Submit record to Github and FAIRsharing",response_class=JSONResponse)
async def submit_record(input_json: dict = Body(...)):
    """Authenticate with Github, then FAIRsharing:
    First Upload an RDF record (in Turtle format) to the OSTrails GitHub repository.
    Automatically creates or updates the corresponding `.ttl` file under its category folder.
    Returns structured feedback including commit URLs and record identifiers.
    Then, for FAIRsharing, resolve subject/domain IDs, and submit the JSON-based cleaned record."""

    try:
        # ─────────────────────────────
        # GitHub
        # ─────────────────────────────
        rdf_text = render_turtle_template(input_json)

        async with httpx.AsyncClient(timeout=20.0) as client:
            github_response = await commit_rdf_to_github(client, rdf_text)

        # ─────────────────────────────
        # FAIRsharing
        # ─────────────────────────────
        body_dict = render_json_template(input_json)
        body_dict = await resolve_subject_domain_ids(body_dict)
        body_dict = remove_empty(body_dict)

        async with httpx.AsyncClient() as client:
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json"
            }

            auth = await client.post(
                AUTH_URL,
                headers=headers,
                json={"user": {"login": USERNAME, "password": PASSWORD}},
                timeout=15.0,
            )
            auth.raise_for_status()

            token = auth.json().get("jwt")
            if not token:
                raise HTTPException(500, "Missing jwt token")

            headers["Authorization"] = f"Bearer {token}"

            data_response = await client.post(
                DATA_URL,
                json=body_dict,
                headers=headers
            )
            data_response.raise_for_status()

            fairsharing_response = data_response.json()

        # ─────────────────────────────
        # Return Combined Result
        # ─────────────────────────────
        return {
            "status": "success",
            "github": github_response,
            "fairsharing": {
                "status_code": data_response.status_code,
                "response": fairsharing_response
            }
        }

    except HTTPException as e:
        raise e

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected internal error: {e}"
        )

# For testing the templates:

@app.post("/questionnaire/render/json", response_class=JSONResponse)
async def render_json(input_json: dict = Body(...)):
    """
    Render FAIRsharing-compatible JSON
    """
    rendered = render_json_template(input_json)
    return JSONResponse(content=rendered)


@app.post("/questionnaire/render/turtle", response_class=PlainTextResponse)
async def render_turtle(input_json: dict = Body(...)):
    """
    Render FAIRsharing Turtle RDF
    """
    rendered = render_turtle_template(input_json)
    return PlainTextResponse(content=rendered, media_type="text/turtle")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
