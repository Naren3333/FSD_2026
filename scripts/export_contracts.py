"""Export versioned HTTP and Kafka schemas. Python HTTP schemas come from FastAPI."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UUID = {"type": "string", "format": "uuid"}
TEXT = {"type": "string", "minLength": 1}


def obj(properties, required=None):
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required, "additionalProperties": False}


def array(item, minimum=0, maximum=100):
    return {"type": "array", "items": item, "minItems": minimum, "maxItems": maximum}


def save(path, data):
    (ROOT/path).write_text(json.dumps(data, indent=2)+"\n", encoding="utf-8")


question = obj({"id": UUID, "prompt": {"type": "string", "minLength": 1, "maxLength": 2000}, "type": {"enum": ["multiple_choice", "numerical"]}, "options": {"type": ["array", "null"], "items": TEXT}, "answer": TEXT, "tolerance": {"type": "string"}, "skill_id": UUID})
question_input = obj({k: v for k, v in question["properties"].items() if k != "id"})
public_question = obj({k: v for k, v in question["properties"].items() if k not in ["answer", "tolerance"]})
assessment = obj({"id": UUID, "classroom_id": UUID, "title": TEXT, "questions": array(question, 1, 50)})
public_assessment = {**assessment, "properties": {**assessment["properties"], "questions": array(public_question, 1, 50)}}
evidence = obj({"question_id": UUID, "skill_id": UUID, "correct": {"type": "boolean"}, "awarded": {"type": "integer", "minimum": 0, "maximum": 1}, "possible": {"const": 1}})
chunk = obj({"ordinal": {"type": "integer", "minimum": 0}, "text": TEXT, "start_character": {"type": "integer", "minimum": 0}, "end_character": {"type": "integer", "minimum": 1}})
document = obj({"id": UUID, "classroom_id": UUID, "title": TEXT, "mime_type": {"enum": ["text/plain", "application/pdf"]}, "status": {"enum": ["extracted", "extraction_unsupported"]}, "revision": {"type": "integer", "minimum": 1}, "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"}, "chunks": array(chunk, maximum=10000)}, ["id", "classroom_id", "title", "mime_type", "status", "revision", "sha256"])
classroom = obj({"id": UUID, "name": TEXT, "teacher_id": TEXT})
node = obj({"id": UUID, "kind": {"enum": ["subject", "course", "unit", "topic", "skill", "objective"]}, "name": TEXT, "parent_id": {"type": ["string", "null"], "format": "uuid"}})
evaluation = obj({"id": UUID, "assessment_id": UUID, "classroom_id": UUID, "student_id": TEXT, "status": {"enum": ["draft", "finalized"]}, "evidence": array(evidence, 1, 50)})
error = obj({"error": obj({"code": TEXT, "message": TEXT})})


def operation(method, path, body, response, code=200, query=()):
    parameters = [{"name": "id", "in": "path", "required": True, "schema": UUID}] if "{id}" in path else []
    parameters += [{"name": q, "in": "query", "required": q != "student_id", "schema": TEXT if q in ("student_id", "title") else UUID} for q in query]
    op = {"operationId": method + path.replace("/", "_").replace("{", "").replace("}", ""), "parameters": parameters, "responses": {str(code): {"description": "Success", **({"content": {"application/json": {"schema": response}}} if response else {})}, **{str(s): {"description": label, "content": {"application/json": {"schema": error}}} for s, label in [(401, "Unauthenticated"), (403, "Forbidden"), (404, "Not found"), (409, "State conflict"), (422, "Validation error"), (503, "Dependency unavailable")]}}}
    if body is not None:
        op["requestBody"] = {"required": True, "content": {"application/json": {"schema": body}}}
    return path, method.lower(), op


operations = {
    "identity": [operation("POST", "/v1/me", obj({}), obj({"id": TEXT, "org_id": TEXT, "role": {"enum": ["teacher", "student"]}})), operation("GET", "/v1/classrooms", None, array(classroom)), operation("POST", "/v1/classrooms", obj({"name": {"type": "string", "minLength": 1, "maxLength": 120}}), classroom, 201), operation("GET", "/v1/classrooms/{id}/access", None, obj({"allowed": {"const": True}}), query=["student_id"]), operation("POST", "/v1/classrooms/{id}/enrollments", obj({"student_id": TEXT}), obj({"student_id": TEXT, "classroom_id": UUID}))],
    "curriculum": [operation("GET", "/v1/nodes", None, array(node, maximum=200)), operation("GET", "/v1/nodes/{id}", None, node), operation("POST", "/v1/nodes", obj({k: v for k, v in node["properties"].items() if k != "id"}, ["kind", "name"]), node, 201), operation("PATCH", "/v1/nodes/{id}", obj({"name": TEXT}), obj({"id": UUID, "name": TEXT})), operation("DELETE", "/v1/nodes/{id}", None, None, 204), operation("POST", "/v1/skills/{id}/prerequisites", obj({"prerequisite_id": UUID}), obj({"skill_id": UUID, "prerequisite_id": UUID}))],
    "content": [operation("GET", "/v1/documents", None, array(document), query=["classroom_id"]), operation("GET", "/v1/documents/{id}", None, document), operation("POST", "/v1/documents", None, document, 201, query=["classroom_id", "title"])],
    "assessment": [operation("GET", "/v1/assessments", None, array(obj({"id": UUID, "title": TEXT})), query=["classroom_id"]), operation("POST", "/v1/assessments", obj({"classroom_id": UUID, "title": {"type": "string", "minLength": 1, "maxLength": 160}, "questions": array(question_input, 1, 50)}), assessment, 201), operation("GET", "/v1/assessments/{id}", None, {"oneOf": [assessment, public_assessment]}), operation("POST", "/v1/assessments/{id}/submissions", obj({"answers": {"type": "object", "additionalProperties": TEXT}}), obj({"id": UUID, "status": {"const": "submitted"}}), 201)],
    "grading": [operation("GET", "/v1/evaluations", None, array(evaluation), query=["classroom_id"]), operation("POST", "/v1/evaluations/{id}/finalize", obj({"reason": {"type": "string", "minLength": 1, "maxLength": 1000}, "overrides": {"type": "object", "additionalProperties": {"type": "integer", "minimum": 0, "maximum": 1}}}, ["reason"]), evaluation)],
}
for name, routes in operations.items():
    paths = {}
    for path, method, op in routes:
        paths.setdefault(path, {})[method] = op
    if name == "content":
        paths["/v1/documents"]["post"]["requestBody"] = {"required": True, "content": {kind: {"schema": {"type": "string", "format": "binary", "maxLength": 5242880}} for kind in ["text/plain", "application/pdf"]}}
        paths["/v1/documents/{id}/original"] = {"get": {"parameters": [{"name": "id", "in": "path", "required": True, "schema": UUID}], "responses": {"200": {"description": "Authorized original file", "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}}}}
    for path in ["/health/live", "/health/ready"]:
        paths[path] = {"get": {"security": [], "responses": {"200": {"description": "Healthy"}, "503": {"description": "Essential dependency unavailable"}}}}
    save(f"contracts/http/{name}.openapi.json", {"openapi": "3.1.0", "info": {"title": name, "version": "1.0.0"}, "security": [{"bearerAuth": []}], "components": {"securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}}}, "paths": paths})

payloads = {
    "content.uploaded.v1": obj({"document_id": UUID, "classroom_id": UUID, "org_id": TEXT, "revision": {"type": "integer", "minimum": 1}, "status": {"enum": ["extracted", "extraction_unsupported"]}}),
    "assessment.submitted.v1": obj({"submission_id": UUID, "assessment_id": UUID, "classroom_id": UUID, "org_id": TEXT, "student_id": TEXT, "questions": array(question, 1, 50), "answers": {"type": "object", "additionalProperties": TEXT}}),
    "grading.finalized.v1": obj({"evaluation_id": UUID, "assessment_id": UUID, "classroom_id": UUID, "org_id": TEXT, "student_id": TEXT, "evidence": array(evidence, 1, 50)}),
}
for topic, payload in payloads.items():
    schema = obj({"event_id": UUID, "event_type": {"const": topic}, "schema_version": {"const": 1}, "timestamp": {"type": "string", "format": "date-time"}, "correlation_id": UUID, "resource_id": UUID, "data": payload})
    save(f"contracts/events/{topic}.schema.json", {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": f"urn:learning:{topic}", **schema})

env = dict(os.environ, PYTHONPATH=str(ROOT/"packages/python-platform"))
for name in ["analytics", "ai-core", "question-generation-agent", "student-performance-agent", "question-chatbot-agent"]:
    data = subprocess.check_output([sys.executable, "-c", "import json; from main import app; print(json.dumps(app.openapi()))"], cwd=ROOT/"services"/name, env=env, text=True)
    save(f"contracts/http/{name}.openapi.json", json.loads(data))
print("Exported ten HTTP contracts and three implemented event schemas.")
