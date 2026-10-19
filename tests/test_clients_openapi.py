"""Every settings, rates and clients route is documented in the OpenAPI schema."""

PREFIX = "/api/v1/organizations/{organization_id}"
EXPECTED = {
    ("get", "/vat-rates"),
    ("post", "/vat-rates"),
    ("get", "/vat-rates/{rate_id}"),
    ("patch", "/vat-rates/{rate_id}"),
    ("delete", "/vat-rates/{rate_id}"),
    ("get", "/services"),
    ("post", "/services"),
    ("patch", "/services/{service_id}"),
    ("post", "/services/{service_id}/deactivate"),
    ("delete", "/services/{service_id}"),
    ("get", "/activity-codes"),
    ("post", "/activity-codes"),
    ("delete", "/activity-codes/{activity_id}"),
    ("get", "/sequences"),
    ("patch", "/sequences/{kind}"),
    ("get", "/members/{user_id}/rates"),
    ("post", "/members/{user_id}/rates"),
    ("delete", "/staff-rates/{rate_id}"),
    ("get", "/staff-rates/current"),
    ("get", "/clients/{client_id}/rates"),
    ("post", "/clients/{client_id}/rates"),
    ("patch", "/clients/{client_id}/rates/{rate_id}"),
    ("delete", "/clients/{client_id}/rates/{rate_id}"),
    ("get", "/clients"),
    ("post", "/clients"),
    ("get", "/clients/summary"),
    ("get", "/clients/export"),
    ("get", "/clients/{client_id}"),
    ("patch", "/clients/{client_id}"),
    ("delete", "/clients/{client_id}"),
    ("post", "/clients/{client_id}/archive"),
    ("get", "/clients/{client_id}/contacts"),
    ("post", "/clients/{client_id}/contacts"),
    ("patch", "/clients/{client_id}/contacts/{contact_id}"),
    ("delete", "/clients/{client_id}/contacts/{contact_id}"),
    ("get", "/clients/{client_id}/services"),
    ("post", "/clients/{client_id}/services"),
    ("patch", "/clients/{client_id}/services/{engagement_id}"),
    ("delete", "/clients/{client_id}/services/{engagement_id}"),
    ("post", "/imports/clients"),
}


async def test_routes_are_documented(client):
    schema = (await client.get("/openapi.json")).json()
    for method, path in EXPECTED:
        operation = schema["paths"][PREFIX + path][method]
        assert operation["summary"], (method, path)
        assert len(operation["description"]) > 20, (method, path)
        assert {"401", "404", "422"} <= set(operation["responses"]), (method, path)
        any_member = path.startswith(("/vat-rates", "/services", "/activity-codes", "/sequences"))
        if method != "get" or not any_member:
            assert "403" in operation["responses"], (method, path)
    upload = schema["paths"][PREFIX + "/imports/clients"]["post"]["requestBody"]["content"]
    assert "multipart/form-data" in upload
