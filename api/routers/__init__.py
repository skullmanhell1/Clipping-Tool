"""The API's route modules, one per OpenAPI tag.

``api/main.py`` builds the app and includes each router below; it declares no routes of
its own. The tag *is* the module boundary, and
``tests/test_api_route_table.py::test_every_api_route_has_exactly_one_tag`` keeps it that
way -- an untagged route would have no module to belong to.
"""
