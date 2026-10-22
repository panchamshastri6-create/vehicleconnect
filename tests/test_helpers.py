import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import create_app

def test_app_has_expected_routes():
    app = create_app()
    assert app.test_client().get("/health").status_code == 200
    assert app.test_client().get("/").status_code == 200

def test_health_json():
    app = create_app()
    response = app.test_client().get("/health")
    assert response.get_json()["status"] == "ok"
