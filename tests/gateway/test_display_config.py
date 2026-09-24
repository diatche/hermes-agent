"""Tests for gateway.display_config — per-platform display/verbosity resolver."""


# ---------------------------------------------------------------------------
# Resolver: resolution order
# ---------------------------------------------------------------------------

class TestToolProgressProvenance:
    def test_winning_source_controls_mode_and_intent(self):
        from gateway.display_config import resolve_tool_progress

        cases = [
            ({}, None, ("off", False)),
            ({}, "all", ("all", True)),
            ({"tool_progress": None}, "all", ("all", True)),
            ({"platforms": {"slack": {"tool_progress": None}}}, "off", ("off", True)),
            ({"tool_progress_overrides": {"slack": None}}, "new", ("new", True)),
            ({"tool_progress": False}, "all", ("off", True)),
            ({"tool_progress": "all", "platforms": {"slack": {"tool_progress": None}}}, "off", ("all", True)),
            ({"tool_progress": "off", "tool_progress_overrides": {"slack": "new"}}, "all", ("new", True)),
            ({"tool_progress_overrides": {"slack": "off"}, "platforms": {"slack": {"tool_progress": "all"}}}, None, ("all", True)),
        ]
        for display, env, expected in cases:
            assert resolve_tool_progress({"display": display}, "slack", env) == expected


class TestResolveDisplaySetting:
    """resolve_display_setting() resolves with correct priority."""

    def test_explicit_platform_override_wins(self):
        """display.platforms.<plat>.<key> takes top priority."""
        from gateway.display_config import resolve_display_setting

        config = {
            "display": {
                "tool_progress": "all",
                "platforms": {
                    "telegram": {"tool_progress": "verbose"},
                },
            }
        }
        assert resolve_display_setting(config, "telegram", "tool_progress") == "verbose"

    def test_global_setting_when_no_platform_override(self):
        """Falls back to display.<key> when no platform override exists."""
        from gateway.display_config import resolve_display_setting

        config = {
            "display": {
                "tool_progress": "new",
                "platforms": {},
            }
        }
        assert resolve_display_setting(config, "telegram", "tool_progress") == "new"

    def test_platform_default_when_no_user_config(self):
        """Falls back to built-in platform default."""
        from gateway.display_config import resolve_display_setting

        # Empty config — should get built-in defaults
        config = {}
        # Telegram is a mobile inbox by default — final-answer-first unless
        # explicitly configured otherwise.
        assert resolve_display_setting(config, "telegram", "tool_progress") == "off"
        # Email defaults to tier_minimal → "off"
        assert resolve_display_setting(config, "email", "tool_progress") == "off"

    def test_global_default_for_unknown_platform(self):
        """Unknown platforms get the global defaults."""
        from gateway.display_config import resolve_display_setting

        config = {}
        # Unknown platform, no config → global default "all"
        assert resolve_display_setting(config, "unknown_platform", "tool_progress") == "all"

    def test_tool_progress_boolean_like_strings_normalise(self):
        """Quoted YAML booleans should not unexpectedly enable progress."""
        from gateway.display_config import resolve_display_setting

        assert resolve_display_setting({"display": {"tool_progress": "false"}}, "telegram", "tool_progress") == "off"
        assert resolve_display_setting({"display": {"tool_progress": "0"}}, "telegram", "tool_progress") == "off"
        assert resolve_display_setting({"display": {"tool_progress": "no"}}, "telegram", "tool_progress") == "off"
        assert resolve_display_setting({"display": {"tool_progress": "true"}}, "telegram", "tool_progress") == "all"

    def test_busy_steer_ack_enabled_string_false_normalises(self):
        from gateway.display_config import resolve_display_setting

        config = {"display": {"platforms": {"telegram": {"busy_steer_ack_enabled": "false"}}}}

        assert resolve_display_setting(config, "telegram", "busy_steer_ack_enabled", True) is False

    def test_todo_progress_defaults_off_and_honours_platform_override(self):
        from gateway.display_config import resolve_display_setting

        assert resolve_display_setting({}, "telegram", "todo_progress") is False
        config = {
            "display": {
                "todo_progress": False,
                "platforms": {"telegram": {"todo_progress": "true"}},
            }
        }
        assert resolve_display_setting(config, "telegram", "todo_progress") is True
        assert resolve_display_setting(config, "discord", "todo_progress") is False

    def test_todo_progress_pin_defaults_off_and_honours_telegram_override(self):
        from gateway.display_config import resolve_display_setting

        assert resolve_display_setting({}, "telegram", "todo_progress_pin") is False
        config = {
            "display": {
                "platforms": {"telegram": {"todo_progress_pin": "true"}},
            }
        }
        assert resolve_display_setting(config, "telegram", "todo_progress_pin") is True
        assert resolve_display_setting(config, "discord", "todo_progress_pin") is False

    def test_fallback_parameter_used_last(self):
        """Explicit fallback is used when nothing else matches."""
        from gateway.display_config import resolve_display_setting

        config = {}
        # "nonexistent_key" isn't in any defaults
        result = resolve_display_setting(config, "telegram", "nonexistent_key", "my_fallback")
        assert result == "my_fallback"

    def test_platform_override_only_affects_that_platform(self):
        """Other platforms are unaffected by a specific platform override."""
        from gateway.display_config import resolve_display_setting

        config = {
            "display": {
                "tool_progress": "all",
                "platforms": {
                    "slack": {"tool_progress": "off"},
                },
            }
        }
        assert resolve_display_setting(config, "slack", "tool_progress") == "off"
        assert resolve_display_setting(config, "telegram", "tool_progress") == "all"


# ---------------------------------------------------------------------------
# Backward compatibility: tool_progress_overrides
# ---------------------------------------------------------------------------

class TestBackwardCompat:
    """Legacy tool_progress_overrides is still respected as a fallback."""

    def test_legacy_overrides_read(self):
        """tool_progress_overrides is read when no platforms entry exists."""
        from gateway.display_config import resolve_display_setting

        config = {
            "display": {
                "tool_progress": "all",
                "tool_progress_overrides": {
                    "signal": "off",
                    "telegram": "verbose",
                },
            }
        }
        assert resolve_display_setting(config, "signal", "tool_progress") == "off"
        assert resolve_display_setting(config, "telegram", "tool_progress") == "verbose"


# ---------------------------------------------------------------------------
# YAML normalisation
# ---------------------------------------------------------------------------

class TestYAMLNormalisation:
    """YAML 1.1 quirks (bare off → False, on → True) are handled."""

    def test_tool_progress_false_normalised_to_off(self):
        """YAML's bare `off` parses as False — normalised to 'off' string."""
        from gateway.display_config import resolve_display_setting

        config = {"display": {"tool_progress": False}}
        assert resolve_display_setting(config, "telegram", "tool_progress") == "off"


    def test_only_long_running_visibility_accepts_generic_mode(self):
        from gateway.display_config import resolve_display_setting

        config = {
            "display": {
                "platforms": {
                    "whatsapp": {
                        "thinking_progress": "generic",
                        "interim_assistant_messages": "generic",
                        "long_running_notifications": "generic",
                    }
                }
            }
        }
        assert resolve_display_setting(config, "whatsapp", "thinking_progress") is False
        assert resolve_display_setting(config, "whatsapp", "interim_assistant_messages") is False
        assert resolve_display_setting(config, "whatsapp", "long_running_notifications") == "generic"

    def test_thinking_progress_string_false_normalised_to_false(self):
        from gateway.display_config import resolve_display_setting

        config = {"display": {"platforms": {"whatsapp": {"thinking_progress": "false"}}}}
        assert resolve_display_setting(config, "whatsapp", "thinking_progress") is False


# ---------------------------------------------------------------------------
# Built-in platform defaults (tier system)
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Config migration: tool_progress_overrides → display.platforms
# ---------------------------------------------------------------------------

class TestConfigMigration:
    """Version 16 migration moves tool_progress_overrides into display.platforms."""

    def test_migration_creates_platforms_entries(self, tmp_path, monkeypatch):
        """Old overrides are migrated into display.platforms.<plat>.tool_progress."""
        import yaml

        config_path = tmp_path / "config.yaml"
        config = {
            "_config_version": 15,
            "display": {
                "tool_progress_overrides": {
                    "signal": "off",
                    "telegram": "all",
                },
            },
        }
        config_path.write_text(yaml.dump(config), encoding="utf-8")

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        # Re-import to pick up the new HERMES_HOME
        import importlib
        import hermes_cli.config as cfg_mod
        importlib.reload(cfg_mod)

        result = cfg_mod.migrate_config(interactive=False, quiet=True)
        # Re-read config
        updated = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        platforms = updated.get("display", {}).get("platforms", {})
        assert platforms.get("signal", {}).get("tool_progress") == "off"
        assert platforms.get("telegram", {}).get("tool_progress") == "all"


# ---------------------------------------------------------------------------
# Streaming per-platform (None = follow global)
# ---------------------------------------------------------------------------

class TestStreamingPerPlatform:
    """Streaming per-platform override semantics."""


    def test_explicit_false_disables(self):
        """Explicit False disables streaming for that platform."""
        from gateway.display_config import resolve_display_setting

        config = {
            "display": {
                "platforms": {"telegram": {"streaming": False}},
            }
        }
        assert resolve_display_setting(config, "telegram", "streaming") is False





# ---------------------------------------------------------------------------
# cleanup_progress — opt-in deletion of temporary progress bubbles
# ---------------------------------------------------------------------------

class TestCleanupProgress:
    """``cleanup_progress`` is off by default and resolvable per-platform."""



    def test_yaml_true_string_normalises_to_true(self):
        """String 'true'/'yes'/'on' all resolve to True."""
        from gateway.display_config import resolve_display_setting

        for val in ("true", "yes", "on", "1"):
            config = {
                "display": {
                    "platforms": {"telegram": {"cleanup_progress": val}},
                }
            }
            assert resolve_display_setting(config, "telegram", "cleanup_progress") is True, val


class TestDelegatedTasks:
    """Delegated checklist detail is independently configurable."""

    def test_default_is_off(self):
        from gateway.display_config import resolve_display_setting

        assert resolve_display_setting({}, "telegram", "delegated_tasks") == "off"

    def test_true_normalises_to_count(self):
        from gateway.display_config import resolve_display_setting

        config = {"display": {"delegated_tasks": True}}
        assert resolve_display_setting(config, "telegram", "delegated_tasks") == "count"

    def test_false_normalises_to_off(self):
        from gateway.display_config import resolve_display_setting

        config = {"display": {"delegated_tasks": False}}
        assert resolve_display_setting(config, "telegram", "delegated_tasks") == "off"

    def test_goal_mode_and_platform_override(self):
        from gateway.display_config import resolve_display_setting

        config = {
            "display": {
                "delegated_tasks": "off",
                "platforms": {"telegram": {"delegated_tasks": "goal"}},
            }
        }
        assert resolve_display_setting(config, "telegram", "delegated_tasks") == "goal"

    def test_invalid_value_falls_back_to_off(self):
        from gateway.display_config import resolve_display_setting

        config = {"display": {"delegated_tasks": "verbose"}}
        assert resolve_display_setting(config, "telegram", "delegated_tasks") == "off"
