import httpx

from inference_guard.mock_backend import create_app


async def test_mock_fault_and_reset():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(create_app()), base_url="http://mock"
    ) as client:
        payload = {"inputs": [1, -2, 0.5]}
        response = await client.post("/infer", json=payload)
        assert response.json() == {"model": "double", "outputs": [2, -4, 1]}
        await client.put("/faults", json={"unavailable": True})
        assert (await client.post("/infer", json=payload)).status_code == 503
        # Process liveness remains healthy even during an injected inference outage.
        assert (await client.get("/healthz")).status_code == 200
        await client.put("/faults", json={})
        assert (await client.post("/infer", json=payload)).status_code == 200
