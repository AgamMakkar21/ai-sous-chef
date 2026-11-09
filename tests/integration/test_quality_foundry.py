import copy
import json
from typing import Any

import httpx2
import pytest
from azure.ai.projects import AIProjectClient
from azure.core.credentials import AccessToken
from azure.core.pipeline.transport import HttpRequest, HttpResponse, HttpTransport
from openai import OpenAI
from sous_chef.settings import Settings

from scripts.quality_evidence import load_suite, rows
from scripts.quality_foundry import collect, deployment_versions, submit


class SyntheticCredential:
    def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        return AccessToken("synthetic-token", 4_000_000_000)


class Response(HttpResponse):
    def __init__(self, request: HttpRequest, data: dict[str, Any], status: int = 200) -> None:
        super().__init__(request, None)
        self.status_code = status
        self.headers = {"Content-Type": "application/json"}
        self.content_type = "application/json"
        self.data = json.dumps(data).encode()

    def body(self) -> bytes:
        return self.data

    def json(self) -> Any:
        return json.loads(self.data)


class ProjectTransport(HttpTransport[HttpRequest, HttpResponse]):
    def __init__(self) -> None:
        self.requests: list[HttpRequest] = []

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def __enter__(self) -> "ProjectTransport":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def send(self, request: HttpRequest, **kwargs: Any) -> HttpResponse:
        self.requests.append(request)
        if "/deployments/" in request.url:
            return Response(
                request,
                {
                    "type": "ModelDeployment",
                    "name": request.url.split("/")[-1].split("?")[0],
                    "modelName": "synthetic-model",
                    "modelVersion": "synthetic-model-v1",
                },
            )
        assert "/evaluators/" in request.url
        if request.method == "GET":
            return Response(request, {"name": "builtin", "version": "9"})
        assert request.method == "POST"
        return Response(request, {"name": "custom", "version": "7"}, status=201)


class FoundryTransport:
    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.status = "completed"
        self.fail_submission = False
        self.suite = load_suite()

    def run(self) -> dict[str, Any]:
        return {
            "id": "run-synthetic",
            "created_at": 1,
            "eval_id": "eval-synthetic",
            "object": "eval.run",
            "name": "synthetic",
            "model": "judge-deployment",
            "status": self.status,
            "error": {"code": "", "message": ""},
            "data_source": {"type": "jsonl", "source": {"type": "file_content", "content": []}},
            "metadata": {},
            "per_model_usage": [
                {
                    "model_name": "synthetic-judge",
                    "prompt_tokens": 1000,
                    "completion_tokens": 500,
                    "cached_tokens": 0,
                    "total_tokens": 1500,
                    "invocation_count": 60,
                }
            ],
            "per_testing_criteria_results": [],
            "report_url": "https://foundry.example.invalid/synthetic",
            "result_counts": {"total": 12, "passed": 10, "failed": 2, "errored": 0},
        }

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path.endswith("/evals"):
            return httpx2.Response(
                200,
                json={
                    "id": "eval-synthetic",
                    "object": "eval",
                    "created_at": 1,
                    **json.loads(request.content),
                },
            )
        if request.method == "POST" and path.endswith("/runs"):
            if self.fail_submission:
                raise httpx2.ReadTimeout("private-upstream-body", request=request)
            return httpx2.Response(200, json=self.run())
        if path.endswith("/runs/run-synthetic"):
            return httpx2.Response(200, json=self.run())
        assert path.endswith("/output_items")
        offset = 6 if request.url.params.get("after") else 0
        scores = {
            **{case.id: 4 for case in self.suite.cases},
            **{anchor.id: anchor.minimum for anchor in self.suite.anchors},
        }
        items: list[dict[str, Any]] = [
            {
                "datasource_item": row,
                "status": "pass" if scores[row["case_id"]] >= 4 else "fail",
                "results": [
                    {
                        "name": metric,
                        "score": scores[row["case_id"]],
                        "passed": scores[row["case_id"]] >= 4,
                        "reason": "Synthetic grade",
                    }
                    for metric in (
                        "relevance",
                        "groundedness",
                        "grocery_utilization",
                        "feasibility",
                        "controlled_creativity",
                    )
                ],
            }
            for row in rows(self.suite, [])
        ][offset : offset + 6]
        for index, item in enumerate(items, start=offset):
            item.update(
                {
                    "id": f"output-{index}",
                    "created_at": 1,
                    "datasource_item_id": index,
                    "eval_id": "eval-synthetic",
                    "run_id": "run-synthetic",
                    "object": "eval.run.output_item",
                    "sample": {"output": [], "input": [], "usage": {}},
                }
            )
        return httpx2.Response(
            200,
            json={
                "object": "list",
                "data": items,
                "has_more": offset == 0,
                "first_id": items[0]["id"],
                "last_id": items[-1]["id"],
            },
        )

    def client(self) -> OpenAI:
        return OpenAI(
            api_key="synthetic",
            base_url="https://foundry.example.invalid/openai/v1",
            max_retries=0,
            http_client=httpx2.Client(transport=httpx2.MockTransport(self.handle)),
        )


def test_pinned_sdk_submits_supported_evaluators_and_inline_data_then_exports_all_pages() -> None:
    suite = load_suite()
    project_transport, transport = ProjectTransport(), FoundryTransport()
    state: dict[str, Any] = {}
    saved: list[dict[str, Any]] = []
    with (
        AIProjectClient(
            endpoint="https://project.example.invalid/api/projects/test",
            credential=SyntheticCredential(),
            transport=project_transport,
            retry_total=0,
        ) as project,
        transport.client() as client,
    ):
        versions = deployment_versions(
            project,
            Settings(
                environment="local",
                adapter_mode="local",
                agents_enabled=False,
                foundry_orchestrator_deployment="orchestrator-model",
                foundry_recipe_nutrition_deployment="recipe-model",
            ),
            "judge-deployment",
        )
        assert versions["judge"]["version"] == "synthetic-model-v1"
        submit(
            project,
            client,
            suite,
            rows(suite, []),
            "judge-deployment",
            state,
            lambda value: saved.append(copy.deepcopy(value)),
        )
        outputs = collect(client, state, lambda value: None, expected_rows=12, wait_seconds=1)
    assert len(outputs) == 12
    assert outputs[0]["results"][0]["reason"] == "Synthetic grade"
    assert "sample" not in outputs[0]
    assert state["status"] == "completed" and state["run_id"] == "run-synthetic"
    assert state["judge_usage"][0]["prompt_tokens"] == 1000
    assert saved[0]["submission_started"] and "run_id" not in saved[0]
    posted = [
        json.loads(request.content) for request in transport.requests if request.method == "POST"
    ]
    criteria = {item["name"]: item for item in posted[0]["testing_criteria"]}
    assert len(criteria) == 5
    assert criteria["relevance"]["evaluator_version"] == "9"
    assert criteria["groundedness"]["data_mapping"]["response"] == "{{item.grounded_response}}"
    assert criteria["feasibility"]["evaluator_version"] == "7"
    assert criteria["feasibility"]["initialization_parameters"] == {
        "deployment_name": "judge-deployment",
        "threshold": 4,
    }
    data = posted[1]["data_source"]
    assert data["type"] == "jsonl" and data["source"]["type"] == "file_content"
    assert len(data["source"]["content"]) == 12
    registrations = [request for request in project_transport.requests if request.method == "POST"]
    assert len(registrations) == 3
    for request in registrations:
        assert isinstance(request.body, (str, bytes))
        definition = json.loads(request.body)["definition"]
        assert definition["type"] == "prompt"
        assert "untrusted data" in definition["prompt_text"]
        assert definition["metrics"]["result"]["threshold"] == 4
    pages = [
        request for request in transport.requests if request.url.path.endswith("/output_items")
    ]
    assert len(pages) == 2 and pages[1].url.params["after"] == "output-5"


def test_ambiguous_submission_is_persisted_and_never_automatically_retried() -> None:
    suite, transport = load_suite(), FoundryTransport()
    transport.fail_submission = True
    state: dict[str, Any] = {}
    with (
        AIProjectClient(
            endpoint="https://project.example.invalid/api/projects/test",
            credential=SyntheticCredential(),
            transport=ProjectTransport(),
            retry_total=0,
        ) as project,
        transport.client() as client,
    ):
        with pytest.raises(Exception, match="timed out"):
            submit(project, client, suite, rows(suite, []), "judge", state, lambda value: None)
        assert state["submission_started"] and state["eval_id"] == "eval-synthetic"
        assert "run_id" not in state
        with pytest.raises(ValueError, match="already attempted"):
            submit(project, client, suite, rows(suite, []), "judge", state, lambda value: None)
        with pytest.raises(ValueError, match="ambiguous"):
            collect(client, state, lambda value: None, expected_rows=12, wait_seconds=0)
    creates = [
        request
        for request in transport.requests
        if request.method == "POST" and request.url.path.endswith("/runs")
    ]
    assert len(creates) == 1


def test_bounded_collection_resumes_the_same_run_without_submitting_work() -> None:
    transport = FoundryTransport()
    transport.status = "in_progress"
    state: dict[str, Any] = {"eval_id": "eval-synthetic", "run_id": "run-synthetic"}
    with transport.client() as client:
        assert collect(client, state, lambda value: None, expected_rows=12, wait_seconds=0) == []
        assert state["status"] == "in_progress"
        transport.status = "completed"
        assert (
            len(collect(client, state, lambda value: None, expected_rows=12, wait_seconds=1)) == 12
        )
    assert all(request.method == "GET" for request in transport.requests)
