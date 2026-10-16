import asyncio
from collections import deque

import pytest
from sous_chef.api.limits import MAX_BODY_BYTES, ProductBodyLimitMiddleware
from starlette.types import Message, Receive, Scope, Send


@pytest.mark.parametrize("oversized", [False, True])
def test_streaming_body_limit_buffers_only_a_bounded_body(oversized: bool) -> None:
    async def scenario() -> None:
        scope: Scope = {
            "type": "http",
            "path": "/api/v1/conversations",
            "method": "POST",
            "headers": [],
        }
        requests: deque[Message] = deque(
            [
                {"type": "http.request", "body": b"a" * (MAX_BODY_BYTES - 1), "more_body": True},
                {
                    "type": "http.request",
                    "body": b"bc" if oversized else b"b",
                    "more_body": oversized,
                },
            ]
        )
        if oversized:
            requests.append({"type": "http.request", "body": b"never-read", "more_body": False})
        responses: list[Message] = []
        received = 0
        downstream_calls = 0

        async def receive() -> Message:
            nonlocal received
            received += 1
            return requests.popleft()

        async def send(message: Message) -> None:
            responses.append(message)

        async def downstream(_scope: Scope, receive: Receive, send: Send) -> None:
            nonlocal downstream_calls
            downstream_calls += 1
            message = await receive()
            assert message["body"] == b"a" * (MAX_BODY_BYTES - 1) + b"b"
            assert message["more_body"] is False
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        await ProductBodyLimitMiddleware(downstream)(scope, receive, send)
        assert received == 2
        assert downstream_calls == (0 if oversized else 1)
        assert responses[0]["status"] == (413 if oversized else 204)
        assert len(requests) == (1 if oversized else 0)

    asyncio.run(scenario())
