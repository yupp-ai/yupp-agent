"""Unit tests for SAG mention_commands module.

Covers the parsing + formatting surface used by ``events.handle_app_mention``:
- ``extract_bare_command`` (stop / attach / help / agents / models / status /
  verbose / quiet)
- ``extract_model_directive`` and ``extract_agent_directive`` (leading
  directive parsing)
- ``extract_leading_directives`` (stacked directives in any order)
- ``format_models_list``, ``format_agents_list`` (mrkdwn formatters)

End-to-end dispatch tests (``dispatch_bare_command`` routing to handlers) live
alongside their handlers' integration tests in test_events_extended.py.
"""

from __future__ import annotations

from ypl.slack_agent_gateway.mention_commands import (
    extract_agent_directive,
    extract_bare_command,
    extract_leading_directives,
    extract_model_directive,
    format_agents_list,
    format_models_list,
)

# ---------------------------------------------------------------------------
# extract_bare_command
# ---------------------------------------------------------------------------


class TestExtractBareCommand:
    def test_stop_command(self) -> None:
        assert extract_bare_command("<@U123> /stop") == ("stop", "")

    def test_stop_command_uppercase(self) -> None:
        assert extract_bare_command("<@U123> /STOP") == ("stop", "")

    def test_stop_without_slash(self) -> None:
        assert extract_bare_command("<@U123> stop") == ("stop", "")

    def test_attach_without_args(self) -> None:
        result = extract_bare_command("<@U123> /attach")
        assert result == ("attach", "")

    def test_attach_with_session_id(self) -> None:
        result = extract_bare_command("<@U123> /attach abc-uuid-123")
        assert result is not None
        assert result[0] == "attach"
        assert result[1] == "abc-uuid-123"

    def test_non_command_returns_none(self) -> None:
        assert extract_bare_command("<@U123> hello world") is None

    def test_regular_message_returns_none(self) -> None:
        assert extract_bare_command("<@U123> can you help me?") is None

    def test_empty_text_returns_none(self) -> None:
        assert extract_bare_command("") is None

    def test_attach_without_slash(self) -> None:
        result = extract_bare_command("<@U123> attach abc-uuid")
        assert result is not None
        assert result[0] == "attach"

    def test_mention_stripped_before_matching(self) -> None:
        # Multiple mentions stripped
        assert extract_bare_command("<@U1> <@U2> stop") == ("stop", "")

    def test_attach_colon_alias(self) -> None:
        # Colon form is accepted as a shorthand for /attach <uuid>.
        assert extract_bare_command("<@U123> /attach:abc-uuid-123") == ("attach", "abc-uuid-123")

    def test_attach_colon_with_empty_uuid(self) -> None:
        # Malformed colon form with no value — caller will reject this as missing UUID.
        assert extract_bare_command("<@U123> /attach:") == ("attach", "")

    def test_help_command(self) -> None:
        assert extract_bare_command("<@U123> /help") == ("help", "")

    def test_help_requires_slash(self) -> None:
        # Bare "help" (no slash) must NOT be treated as a command — it's a common
        # natural-language phrasing ("help me with X").
        assert extract_bare_command("<@U123> help me with this") is None
        assert extract_bare_command("<@U123> help") is None

    def test_agents_command(self) -> None:
        assert extract_bare_command("<@U123> /agents") == ("agents", "")

    def test_models_command(self) -> None:
        assert extract_bare_command("<@U123> /models") == ("models", "")

    def test_status_command(self) -> None:
        assert extract_bare_command("<@U123> /status") == ("status", "")

    def test_status_requires_slash(self) -> None:
        # "status" alone is a common word; don't accidentally match it as a command.
        assert extract_bare_command("<@U123> status") is None
        assert extract_bare_command("<@U123> status update") is None

    def test_verbose_command(self) -> None:
        assert extract_bare_command("<@U123> /verbose") == ("verbose", "")

    def test_quiet_command(self) -> None:
        assert extract_bare_command("<@U123> /quiet") == ("quiet", "")

    def test_verbose_and_quiet_require_slash(self) -> None:
        assert extract_bare_command("<@U123> verbose") is None
        assert extract_bare_command("<@U123> quiet") is None

    def test_extra_args_after_bare_command_ignored(self) -> None:
        # /help foo bar should match /help; extras are dropped.
        assert extract_bare_command("<@U123> /help foo bar") == ("help", "")

    def test_case_insensitive_new_commands(self) -> None:
        assert extract_bare_command("<@U123> /HELP") == ("help", "")
        assert extract_bare_command("<@U123> /Agents") == ("agents", "")
        assert extract_bare_command("<@U123> /StAtUs") == ("status", "")


# ---------------------------------------------------------------------------
# extract_model_directive
# ---------------------------------------------------------------------------


class TestExtractModelDirective:
    def test_no_directive_returns_none_spec(self) -> None:
        spec, text = extract_model_directive("<@U123> please review my PR")
        assert spec is None
        assert "please review my PR" in text

    def test_model_directive_extracted(self) -> None:
        spec, text = extract_model_directive("<@U123> /model:anthropic/claude-3-5 do this task")
        assert spec == "anthropic/claude-3-5"
        assert text == "do this task"

    def test_model_directive_case_insensitive(self) -> None:
        spec, _ = extract_model_directive("<@U123> /Model:openai/gpt-4 hello")
        assert spec == "openai/gpt-4"

    def test_model_only_no_remaining_text(self) -> None:
        spec, text = extract_model_directive("<@U123> /model:mymodel/v1")
        assert spec == "mymodel/v1"
        assert text == ""

    def test_mention_stripped_from_cleaned_text(self) -> None:
        _, text = extract_model_directive("<@UABC> no directive here")
        assert "<@UABC>" not in text

    def test_model_directive_not_first_word_ignored(self) -> None:
        """If /model: is not the first word, it's treated as normal text."""
        spec, text = extract_model_directive("<@U123> hello /model:foo do stuff")
        assert spec is None
        assert "/model:foo" in text

    def test_model_directive_space_form(self) -> None:
        """Space form ``/model SPEC rest`` is accepted for syntax consistency."""
        spec, text = extract_model_directive("<@U123> /model anthropic/claude-3-5 do this task")
        assert spec == "anthropic/claude-3-5"
        assert text == "do this task"

    def test_model_directive_space_form_case_insensitive(self) -> None:
        spec, _ = extract_model_directive("<@U123> /Model openai/gpt-4 hello")
        assert spec == "openai/gpt-4"

    def test_model_directive_space_form_no_value(self) -> None:
        """Bare ``/model`` with no value is not a directive — treat as natural text."""
        spec, text = extract_model_directive("<@U123> /model")
        assert spec is None
        assert "/model" in text

    def test_model_directive_colon_form_no_value(self) -> None:
        """Bare ``/model:`` with empty value is not a directive."""
        spec, text = extract_model_directive("<@U123> /model:")
        assert spec is None
        assert "/model:" in text


# ---------------------------------------------------------------------------
# extract_agent_directive
# ---------------------------------------------------------------------------


class TestExtractAgentDirective:
    def test_no_directive_returns_none(self) -> None:
        name, text = extract_agent_directive("<@U123> please look at this")
        assert name is None
        assert "please look at this" in text

    def test_colon_form(self) -> None:
        name, text = extract_agent_directive("<@U123> /agent:sre what's firing?")
        assert name == "sre"
        assert text == "what's firing?"

    def test_space_form(self) -> None:
        name, text = extract_agent_directive("<@U123> /agent code-reviewer run a query")
        assert name == "code-reviewer"
        assert text == "run a query"

    def test_case_insensitive(self) -> None:
        name, _ = extract_agent_directive("<@U123> /Agent SRE hello")
        assert name == "SRE"  # value case preserved; only command name is case-insensitive

    def test_only_first_word_matters(self) -> None:
        name, text = extract_agent_directive("<@U123> hello /agent sre do stuff")
        assert name is None
        assert "/agent sre do stuff" in text

    def test_no_value_not_a_directive(self) -> None:
        name, text = extract_agent_directive("<@U123> /agent")
        assert name is None
        assert "/agent" in text

    def test_bang_alias_with_space_after_mention(self) -> None:
        """``@raccoon !sre rest`` → ("sre", "rest")."""
        name, text = extract_agent_directive("<@U123> !sre what's firing?")
        assert name == "sre"
        assert text == "what's firing?"

    def test_bang_alias_no_space_after_mention(self) -> None:
        """``@raccoon!sre rest`` → ("sre", "rest"). MENTION_PATTERN strips the
        ``<@...>`` whether or not whitespace follows, so this collapses to the
        same input as the spaced form.
        """
        name, text = extract_agent_directive("<@U123>!sre what's firing?")
        assert name == "sre"
        assert text == "what's firing?"

    def test_bang_alias_with_hyphenated_name(self) -> None:
        name, text = extract_agent_directive("<@U123> !code-reviewer run a query")
        assert name == "code-reviewer"
        assert text == "run a query"

    def test_bang_alias_only_no_remaining_text(self) -> None:
        name, text = extract_agent_directive("<@U123> !sre")
        assert name == "sre"
        assert text == ""

    def test_bare_bang_not_a_directive(self) -> None:
        """A lone ``!`` (with whitespace before the name) is not a directive."""
        name, text = extract_agent_directive("<@U123> ! sre hello")
        assert name is None
        # The bare ``!`` survives as plain text in the cleaned output.
        assert text.startswith("!")

    def test_bang_alias_only_first_word_matters(self) -> None:
        """``!name`` later in the message is not a directive."""
        name, text = extract_agent_directive("<@U123> hello !sre do stuff")
        assert name is None
        assert "!sre" in text


# ---------------------------------------------------------------------------
# extract_leading_directives
# ---------------------------------------------------------------------------


class TestExtractLeadingDirectives:
    def test_neither_present(self) -> None:
        agent, model, cleaned = extract_leading_directives("<@U1> hello")
        assert agent is None
        assert model is None
        assert cleaned == "hello"

    def test_only_agent(self) -> None:
        agent, model, cleaned = extract_leading_directives("<@U1> /agent sre what alerts")
        assert agent == "sre"
        assert model is None
        assert cleaned == "what alerts"

    def test_only_model(self) -> None:
        agent, model, cleaned = extract_leading_directives("<@U1> /model anthropic/claude-sonnet-4-6 review my PR")
        assert agent is None
        assert model == "anthropic/claude-sonnet-4-6"
        assert cleaned == "review my PR"

    def test_agent_then_model(self) -> None:
        agent, model, cleaned = extract_leading_directives(
            "<@U1> /agent sre /model anthropic/claude-sonnet-4-6 what's firing?"
        )
        assert agent == "sre"
        assert model == "anthropic/claude-sonnet-4-6"
        assert cleaned == "what's firing?"

    def test_model_then_agent(self) -> None:
        agent, model, cleaned = extract_leading_directives(
            "<@U1> /model anthropic/claude-sonnet-4-6 /agent sre what's firing?"
        )
        assert agent == "sre"
        assert model == "anthropic/claude-sonnet-4-6"
        assert cleaned == "what's firing?"

    def test_colon_and_space_mixed(self) -> None:
        agent, model, cleaned = extract_leading_directives("<@U1> /agent:sre /model anthropic/claude-sonnet-4-6 go")
        assert agent == "sre"
        assert model == "anthropic/claude-sonnet-4-6"
        assert cleaned == "go"

    def test_stacked_no_body(self) -> None:
        agent, model, cleaned = extract_leading_directives("<@U1> /agent sre /model anthropic/claude-sonnet-4-6")
        assert agent == "sre"
        assert model == "anthropic/claude-sonnet-4-6"
        assert cleaned == ""

    def test_bang_alias_only(self) -> None:
        """``!sre rest`` is parsed as agent=sre with no model directive."""
        agent, model, cleaned = extract_leading_directives("<@U1> !sre what alerts")
        assert agent == "sre"
        assert model is None
        assert cleaned == "what alerts"

    def test_bang_alias_no_space_after_mention(self) -> None:
        """``@raccoon!sre`` (no space) parses the same as ``@raccoon !sre``."""
        agent, model, cleaned = extract_leading_directives("<@U1>!sre what alerts")
        assert agent == "sre"
        assert model is None
        assert cleaned == "what alerts"

    def test_bang_alias_then_model(self) -> None:
        agent, model, cleaned = extract_leading_directives(
            "<@U1> !sre /model anthropic/claude-sonnet-4-6 what's firing?"
        )
        assert agent == "sre"
        assert model == "anthropic/claude-sonnet-4-6"
        assert cleaned == "what's firing?"

    def test_model_then_bang_alias(self) -> None:
        agent, model, cleaned = extract_leading_directives(
            "<@U1> /model anthropic/claude-sonnet-4-6 !sre what's firing?"
        )
        assert agent == "sre"
        assert model == "anthropic/claude-sonnet-4-6"
        assert cleaned == "what's firing?"


# ---------------------------------------------------------------------------
# format_models_list
# ---------------------------------------------------------------------------


class TestFormatModelsList:
    def test_contains_harnessed_section(self) -> None:
        models = {"harnessed": ["claude-agent", "codex-agent"], "raw": []}
        result = format_models_list(models)
        assert "Harnessed" in result
        assert "claude-agent" in result
        assert "codex-agent" in result

    def test_contains_raw_section(self) -> None:
        models = {"harnessed": [], "raw": ["gpt-4o", "claude-3-5-sonnet"]}
        result = format_models_list(models)
        assert "Raw LLM" in result
        assert "gpt-4o" in result

    def test_usage_example_included(self) -> None:
        models: dict[str, list[str]] = {"harnessed": [], "raw": []}
        result = format_models_list(models)
        # Space form is the canonical documented syntax.
        assert "/model " in result

    def test_empty_models_dict(self) -> None:
        result = format_models_list({})
        # Should not raise, just return formatted empty sections
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# format_agents_list
# ---------------------------------------------------------------------------


class TestFormatAgentsList:
    def test_empty_list(self) -> None:
        assert "No agents available" in format_agents_list([])

    def test_renders_agent_names_and_models(self) -> None:
        agents = [
            {
                "name": "sre",
                "display_name": "SRE",
                "description": "Site reliability engineer",
                "executor_model": "claude-code-cli",
            },
            {
                "name": "code-reviewer",
                "display_name": "Data Scientist",
                "description": "Runs SQL and analyses",
                "llm_model": "openai/gpt-4o",
            },
        ]
        result = format_agents_list(agents)
        assert "sre" in result
        assert "SRE" in result
        assert "claude-code-cli" in result
        assert "code-reviewer" in result
        assert "openai/gpt-4o" in result

    def test_usage_hint_included(self) -> None:
        result = format_agents_list([{"name": "x", "display_name": "X"}])
        assert "/agent" in result
        # The bang-form shorthand is also documented.
        assert "!AGENT_NAME" in result

    def test_description_truncated_when_long(self) -> None:
        long_desc = "a" * 200
        result = format_agents_list([{"name": "x", "display_name": "X", "description": long_desc}])
        # Description is truncated with ellipsis rather than dumped verbatim.
        assert "..." in result
        assert long_desc not in result
