"""
Unit tests for api/model_sync.py bundle packaging, validation, database publish, and synchronization.
"""

from pathlib import Path
import pytest

from api.model_sync import (
    BUNDLE_KEY,
    ALLOWED_FILES,
    WHITELIST,
    build_bundle,
    validate_bundle,
    publish_bundle_to_db,
    publish_models_dir,
    read_bundle_from_db,
    sync_from_db,
)


def test_build_and_validate_bundle():
    fake_files = {
        "xgb_model.pkl": b"fake_xgb_content",
        "lgbm_model.pkl": b"fake_lgbm_content",
        "model_meta.json": b'{"version": 1}',
    }
    bundle = build_bundle(fake_files, metrics={"val_accuracy": 0.58}, source="test_runner")
    assert "version" in bundle
    assert "files" in bundle
    assert "xgb_model.pkl" in bundle["files"]
    assert "sha256" in bundle["files"]["xgb_model.pkl"]
    assert bundle["metrics"]["val_accuracy"] == 0.58

    ok, reason = validate_bundle(bundle)
    assert ok, f"Validation failed: {reason}"


def test_build_bundle_rejects_non_whitelisted_file():
    fake_files = {
        "malicious_script.sh": b"rm -rf /",
    }
    with pytest.raises(ValueError, match="Unsupported model artifacts"):
        build_bundle(fake_files, metrics={"val_accuracy": 0.60})


def test_validate_bundle_rejects_sub_par_accuracy():
    fake_files = {
        "xgb_model.pkl": b"fake_xgb",
    }
    # Below MIN_VAL_ACCURACY (0.50)
    bundle = build_bundle(fake_files, metrics={"val_accuracy": 0.42})
    ok, reason = validate_bundle(bundle)
    assert not ok
    assert "below gate" in reason


def test_publish_and_read_bundle_roundtrip(monkeypatch):
    stored_state = {}

    def fake_set_runtime_state(key, value):
        stored_state[key] = value

    def fake_get_runtime_state(key, default=None):
        return stored_state.get(key, default)

    monkeypatch.setattr("db.runtime_state.set_runtime_state", fake_set_runtime_state)
    monkeypatch.setattr("db.runtime_state.get_runtime_state", fake_get_runtime_state)

    fake_files = {
        "xgb_model.pkl": b"xgb_binary_data",
        "calibrator.pkl": b"calibrator_binary_data",
    }
    bundle = build_bundle(fake_files, metrics={"val_accuracy": 0.55}, source="colab_test")
    publish_bundle_to_db(bundle)

    read_back = read_bundle_from_db()
    assert read_back is not None
    assert read_back["source"] == "colab_test"
    assert read_back["version"] == bundle["version"]
    assert "xgb_model.pkl" in read_back["files"]


def test_publish_models_dir(tmp_path, monkeypatch):
    stored_state = {}

    def fake_set_runtime_state(key, value):
        stored_state[key] = value

    def fake_get_runtime_state(key, default=None):
        return stored_state.get(key, default)

    monkeypatch.setattr("db.runtime_state.set_runtime_state", fake_set_runtime_state)
    monkeypatch.setattr("db.runtime_state.get_runtime_state", fake_get_runtime_state)

    # Create dummy models directory with whitelisted files
    (tmp_path / "xgb_model.pkl").write_bytes(b"dummy_xgb")
    (tmp_path / "lgbm_model.pkl").write_bytes(b"dummy_lgbm")
    (tmp_path / "model_meta.json").write_text('{"name": "test"}')

    res = publish_models_dir(tmp_path, metrics={"val_accuracy": 0.56}, source="kaggle")
    assert res["ok"] is True
    assert res["files_count"] == 3
    assert res["source"] == "kaggle"

    # Verify sync_from_db applies it cleanly to another directory
    sync_target = tmp_path / "target_sync"
    sync_res = sync_from_db(target_dir=sync_target)
    assert sync_res["ok"] is True
    assert sync_res["applied"] is True
    assert (sync_target / "xgb_model.pkl").read_bytes() == b"dummy_xgb"
    assert (sync_target / "lgbm_model.pkl").read_bytes() == b"dummy_lgbm"
