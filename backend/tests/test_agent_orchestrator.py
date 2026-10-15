import asyncio

from agents.agent_orchestrator import (
    AgentProfile,
    AgentResponse,
    AgentOrchestrator,
    AgentType,
    BillingAgent,
    EscalationAgent,
    GeneralAgent,
    OrchestratorResult,
    Request,
    ResponseComposer,
    RoutingDecision,
    TechnicalAgent,
    build_shared_rag_tools,
)
from agents.tools import build_business_tools
from core.intent_recognizer import IntentCategory
from mcp.tool_manager import MCPToolManager


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

        class Messages:
            async def create(inner, **kwargs):
                self.calls.append(kwargs)
                if self.error:
                    raise self.error
                return self.response

        self.messages = Messages()


class FakeBusinessRepository:
    async def get_order_status(self, user_id, order_id):
        return {"success": True, "order_id": order_id, "status": "PAID"}

    async def get_order_payment_summary(self, user_id, order_id):
        return {"success": True, "order_id": order_id, "duplicate_payment_suspected": True}

    async def get_refund_status(self, user_id, order_id):
        return {"success": True, "order_id": order_id, "status": "PROCESSING"}

    async def get_error_code_playbook(self, error_code):
        return {"success": True, "error_code": error_code}

    async def query_incident_status(self, error_code, service_name=""):
        return {"success": True, "error_code": error_code, "status": "RESOLVED"}

    async def create_handoff_ticket(self, **kwargs):
        return {"success": True, "ticket_id": "TKT-UNIT-001", "priority": kwargs["priority"], "status": "OPEN"}


def configure_business_tools(agent, repository=None):
    agent.set_business_tools(build_business_tools(repository or FakeBusinessRepository()))
    return agent


def make_request(**kwargs):
    values = {
        "message": "登录时报 401，同时这笔订单被重复扣款",
        "user_id": "u1",
        "conv_id": "c1",
        "intent": IntentCategory.TECHNICAL_LOGIN,
        "intent_group": "technical",
        "intent_confidence": 0.92,
        "entities": {"error_code": ["401"], "amount": ["99 元"]},
    }
    values.update(kwargs)
    return Request(**values)


def test_agent_profiles_have_distinct_contracts_and_generation_config():
    assert isinstance(GeneralAgent.profile, AgentProfile)
    assert GeneralAgent.profile.role != TechnicalAgent.profile.role
    assert TechnicalAgent.profile.workflow != BillingAgent.profile.workflow
    assert TechnicalAgent.profile.temperature < GeneralAgent.profile.temperature
    assert "search_knowledge_base" in GeneralAgent.profile.tool_scope
    assert "query_incident_status" in TechnicalAgent.profile.tool_scope
    assert "get_order_payment_summary" in BillingAgent.profile.tool_scope
    assert GeneralAgent.profile.max_tokens == TechnicalAgent.profile.max_tokens == BillingAgent.profile.max_tokens == 8192


def test_intent_embedding_uses_local_bge_service_when_available():
    from core.intent_recognizer import IntentRecognizer

    recognizer = IntentRecognizer(api_key="test-key", model="test-model")

    async def fake_request(texts):
        return [[float(index), 1.0] for index, _ in enumerate(texts)]

    recognizer._request_embeddings = fake_request

    vectors = asyncio.run(recognizer._embed_texts(["申请退款", "登录失败"]))

    assert vectors == [[0.0, 1.0], [1.0, 1.0]]


def test_intent_embedding_falls_back_when_local_bge_service_is_unavailable():
    from core.intent_recognizer import IntentRecognizer

    recognizer = IntentRecognizer(api_key="test-key", model="test-model")

    async def unavailable_request(texts):
        raise RuntimeError("embedding service unavailable")

    recognizer._request_embeddings = unavailable_request

    vector = asyncio.run(recognizer._embed_text("申请退款"))

    assert len(vector) == 256


def test_intent_templates_keep_parent_and_child_examples_disjoint():
    from core.intent_recognizer import IntentCategory, _TEMPLATES

    parent_children = {
        IntentCategory.BILLING: {IntentCategory.REFUND, IntentCategory.INVOICE, IntentCategory.PAYMENT_ISSUE},
        IntentCategory.TECHNICAL: {IntentCategory.TECHNICAL_LOGIN, IntentCategory.TECHNICAL_CRASH},
        IntentCategory.ACCOUNT: {IntentCategory.ACCOUNT_SECURITY},
        IntentCategory.ESCALATION: {IntentCategory.HUMAN_HANDOFF},
    }
    for parent, children in parent_children.items():
        parent_templates = {text.lower() for text in _TEMPLATES[parent]}
        for child in children:
            assert parent_templates.isdisjoint(text.lower() for text in _TEMPLATES[child])


def test_embedding_prefers_near_tied_specific_child_over_parent():
    from core.intent_recognizer import IntentCategory, IntentRecognizer

    recognizer = IntentRecognizer(api_key="test-key", model="test-model")
    recognizer._tpl_embeddings = {
        IntentCategory.BILLING: [[1.0, 0.0]],
        IntentCategory.REFUND: [[0.9998, 0.02]],
    }

    async def no_load():
        return None

    async def fake_embed(_text):
        return [1.0, 0.0]

    recognizer._load_template_embeddings = no_load
    recognizer._embed_text = fake_embed

    result = asyncio.run(recognizer._embedding_recognize("我想申请退款"))

    assert result["intent"] == IntentCategory.REFUND


def test_embedding_keeps_parent_when_child_score_is_not_near():
    from core.intent_recognizer import IntentCategory, IntentRecognizer

    recognizer = IntentRecognizer(api_key="test-key", model="test-model")
    recognizer._tpl_embeddings = {
        IntentCategory.BILLING: [[1.0, 0.0]],
        IntentCategory.REFUND: [[0.7, 0.714]],
    }

    async def no_load():
        return None

    async def fake_embed(_text):
        return [1.0, 0.0]

    recognizer._load_template_embeddings = no_load
    recognizer._embed_text = fake_embed

    result = asyncio.run(recognizer._embedding_recognize("我想查看账单"))

    assert result["intent"] == IntentCategory.BILLING


def test_intent_result_records_each_source_decision():
    from core.intent_recognizer import IntentRecognizer

    recognizer = IntentRecognizer(api_key="test-key", model="test-model")

    async def fake_llm(_message, _history):
        return {"intent": IntentCategory.REFUND, "confidence": 1.0, "reasoning": "退款诉求"}

    async def fake_embedding(_message):
        return {"intent": IntentCategory.BILLING, "confidence": 0.72}

    recognizer._llm_recognize = fake_llm
    recognizer._embedding_recognize = fake_embedding

    result = asyncio.run(recognizer.recognize("我想申请退款"))

    assert result.decision == {
        "llm": {"intent": "refund", "confidence": 1.0},
        "embedding": {"intent": "billing", "confidence": 0.72},
        "pattern": {"intent": "refund", "confidence": 0.5},
        "final": {"intent": "refund", "confidence": 0.75},
    }


def test_domain_agents_build_different_role_packets():
    req = make_request()
    general_packet = GeneralAgent(FakeClient(), "test-model")._build_role_packet(req)
    technical_packet = TechnicalAgent(FakeClient(), "test-model")._build_role_packet(req)
    billing_packet = BillingAgent(FakeClient(), "test-model")._build_role_packet(req)

    assert "triage_targets" in general_packet
    assert "diagnostic_fields" in technical_packet
    assert "verification_fields" in billing_packet
    assert general_packet != technical_packet != billing_packet


def test_escalation_agent_is_a_real_non_llm_handoff_node():
    client = FakeClient()
    agent = EscalationAgent(client, "test-model", business_repository=FakeBusinessRepository())

    result = asyncio.run(agent.handle(make_request(
        intent=IntentCategory.HUMAN_HANDOFF,
    )))

    assert result.success is True
    assert result.escalate is True
    assert "工单号" in result.content
    assert result.tools_used == ["create_handoff_ticket"]
    assert client.calls == []


def test_composer_fallback_preserves_primary_and_supporting_results():
    composer = ResponseComposer(FakeClient(error=RuntimeError("provider down")), "test-model")
    req = make_request()
    responses = [
        AgentResponse(AgentType.TECHNICAL, "先排查 Token 是否过期。", True),
        AgentResponse(AgentType.BILLING, "请提供两笔扣款的时间和金额。", True),
    ]

    composition = asyncio.run(composer.compose(req, responses))
    content = composition.content

    assert content.startswith("先排查 Token 是否过期。")
    assert "补充说明" in content
    assert "两笔扣款" in content
    assert composition.llm_traces[0]["agent_type"] == "composer"
    assert composition.llm_traces[0]["status"] == "failed"


def test_composer_records_successful_llm_call():
    class TextBlock:
        type = "text"
        text = "已合并技术与账单处理建议。"

    composer = ResponseComposer(
        FakeClient(response=type("Response", (), {"content": [TextBlock()]})()),
        "test-model",
    )
    result = asyncio.run(composer.compose(
        make_request(),
        [
            AgentResponse(AgentType.TECHNICAL, "技术建议", True),
            AgentResponse(AgentType.BILLING, "账单建议", True),
        ],
    ))

    assert result.content == "已合并技术与账单处理建议。"
    assert result.llm_traces[0]["agent_type"] == "composer"
    assert result.llm_traces[0]["status"] == "success"
    assert composer._client.calls[0]["max_tokens"] == 8192


def test_empty_agent_text_retries_once_without_tools():
    class ToolUseBlock:
        type = "tool_use"
        id = "toolu_retry"
        name = "get_error_code_playbook"
        input = {"error_code": "500"}

    class TextBlock:
        type = "text"

        def __init__(self, text):
            self.text = text

    class RetryClient:
        def __init__(self):
            self.calls = []
            self.responses = [
                type("Response", (), {"content": [ToolUseBlock()]})(),
                type("Response", (), {"content": []})(),
                type("Response", (), {"content": [TextBlock("已根据工具结果完成排查。")]})(),
            ]

            class Messages:
                async def create(inner, **kwargs):
                    self.calls.append(kwargs)
                    return self.responses.pop(0)

            self.messages = Messages()

    client = RetryClient()
    agent = configure_business_tools(TechnicalAgent(client, "test-model"))
    result = asyncio.run(agent.handle(make_request(entities={"error_code": ["500"]})))

    assert result.success is True
    assert result.content == "已根据工具结果完成排查。"
    assert len(client.calls) == 3
    assert "tools" in client.calls[1]
    assert "tools" not in client.calls[2]
    assert "不要返回空内容" in str(client.calls[2]["messages"])


def test_empty_agent_text_after_retry_is_a_failure():
    client = FakeClient(response=type("Response", (), {"content": []})())
    agent = TechnicalAgent(client, "test-model")

    result = asyncio.run(agent.handle(make_request()))

    assert result.success is False
    assert "未返回最终文本" in result.error
    assert len(client.calls) == 2


def test_routing_decision_can_target_escalation_pool():
    # Keep this assertion close to the public data contract used by the API.
    decision = RoutingDecision(
        primary_agent=AgentType.ESCALATION,
        reason="critical request",
        confidence=1.0,
    )
    assert decision.agent_types == [AgentType.ESCALATION]
    assert not decision.multi_agent


def test_urgent_word_does_not_override_business_route():
    orchestrator = AgentOrchestrator(api_key="test-key", model="test-model")

    urgent_order = make_request(
        message="紧急，帮我查询订单 ORD-DEMO-1004 的状态",
        intent=IntentCategory.ORDER_STATUS,
        intent_group="general",
    )
    handoff = make_request(
        message="请转人工处理",
        intent=IntentCategory.HUMAN_HANDOFF,
        intent_group="escalation",
    )

    assert orchestrator._route_decision(urgent_order).primary_agent == AgentType.GENERAL
    assert orchestrator._route_decision(handoff).primary_agent == AgentType.ESCALATION


def test_composite_problem_routes_to_primary_and_supporting_agents():
    orchestrator = AgentOrchestrator(api_key="test-key", model="test-model")
    request = make_request(
        message="登录后台提示 500，同时订单 ORD-DEMO-1001 被重复扣款 99 元，帮我同时排查技术和账单问题。",
        intent=IntentCategory.REFUND,
        intent_group="billing",
        entities={"order_id": ["ORD-DEMO-1001"], "amount": ["99 元"], "error_code": ["500"]},
    )

    decision = orchestrator._route_decision(request)

    assert decision.primary_agent == AgentType.BILLING
    assert decision.supporting_agents == [AgentType.TECHNICAL]
    assert decision.multi_agent is True
    assert "composite_targets=technical,billing" in decision.reason


def test_login_error_and_refund_request_routes_to_technical_and_billing():
    orchestrator = AgentOrchestrator(api_key="test-key", model="test-model")
    request = make_request(
        message="登录时提示错误，应该怎么排查？我想申请退款，订单号是 ORD-DEMO-1001",
        intent=IntentCategory.REFUND,
        intent_group="billing",
        entities={"order_id": ["ORD-DEMO-1001"]},
    )

    decision = orchestrator._route_decision(request)

    assert decision.primary_agent == AgentType.BILLING
    assert decision.supporting_agents == [AgentType.TECHNICAL]
    assert decision.multi_agent is True
    assert "composite_targets=technical,billing" in decision.reason


def test_composite_problem_runs_agents_in_parallel_and_composes_response():
    class TextBlock:
        type = "text"

        def __init__(self, text):
            self.text = text

    def response(text):
        return type("Response", (), {"content": [TextBlock(text)]})()

    orchestrator = AgentOrchestrator(api_key="test-key", model="test-model")
    billing = BillingAgent(FakeClient(response("账单核验结果")), "test-model")
    technical = TechnicalAgent(FakeClient(response("技术排查结果")), "test-model")
    orchestrator._pool[AgentType.BILLING] = [billing]
    orchestrator._pool[AgentType.TECHNICAL] = [technical]
    orchestrator._composer = ResponseComposer(FakeClient(response("已合并技术和账单结果。")), "test-model")
    request = make_request(
        message="登录后台提示 500，同时订单 ORD-DEMO-1001 被重复扣款 99 元，帮我同时排查技术和账单问题。",
        intent=IntentCategory.REFUND,
        intent_group="billing",
        entities={"order_id": ["ORD-DEMO-1001"], "amount": ["99 元"], "error_code": ["500"]},
    )

    result = asyncio.run(orchestrator.run(request))

    assert result.primary_agent == AgentType.BILLING
    assert result.supporting_agents == [AgentType.TECHNICAL]
    assert result.agent_types == [AgentType.BILLING, AgentType.TECHNICAL]
    assert result.response == "已合并技术和账单结果。"
    assert result.composer_content == "已合并技术和账单结果。"
    assert {trace["agent_type"] for trace in result.llm_traces} == {"billing", "technical", "composer"}
    assert [(trace["agent_type"], trace["status"]) for trace in result.agent_executions] == [
        ("billing", "success"), ("technical", "success"),
    ]


def test_agent_tool_scopes_are_real_and_isolated():
    general_tools = set(configure_business_tools(GeneralAgent(FakeClient(), "test-model")).get_tools())
    technical_tools = set(configure_business_tools(TechnicalAgent(FakeClient(), "test-model")).get_tools())
    billing_tools = set(configure_business_tools(BillingAgent(FakeClient(), "test-model")).get_tools())
    escalation_tools = set(EscalationAgent(FakeClient(), "test-model").get_tools())

    assert general_tools == {"get_order_status", "request_human_handoff"}
    assert technical_tools == {"get_error_code_playbook", "query_incident_status", "request_human_handoff"}
    assert billing_tools == {"get_order_payment_summary", "get_refund_status", "request_human_handoff"}
    assert escalation_tools == set()
    assert general_tools & technical_tools == {"request_human_handoff"}
    assert technical_tools & billing_tools == {"request_human_handoff"}


def test_shared_rag_tool_is_available_to_all_agents():
    class RagManager:
        async def search_with_rewrite(self, tool_name, query, top_k=5):
            return type(
                "Result",
                (),
                {"success": True, "data": [{"title": "退款政策", "content": "7 天内可退款"}], "reranked": True},
            )()

    shared = build_shared_rag_tools(RagManager())

    general = GeneralAgent(FakeClient(), "test-model")
    technical = TechnicalAgent(FakeClient(), "test-model")
    billing = BillingAgent(FakeClient(), "test-model")
    escalation = EscalationAgent(FakeClient(), "test-model")

    for agent in (general, technical, billing):
        agent.set_shared_tools(shared)
        tools = agent.get_tools()
        assert "search_knowledge_base" in tools


def test_tool_input_validation_rejects_unknown_fields():
    agent = configure_business_tools(TechnicalAgent(FakeClient(), "test-model"))
    spec = agent.get_tools()["get_error_code_playbook"]

    try:
        agent._validate_tool_input(spec, {"error_code": "401", "secret": "nope"})
    except ValueError as exc:
        assert "不允许的工具参数" in str(exc)
    else:
        raise AssertionError("unknown tool fields should be rejected")


def test_tool_use_round_trip_executes_only_whitelisted_tool():
    class ToolUseBlock:
        type = "tool_use"
        id = "toolu_1"
        name = "get_error_code_playbook"
        input = {"error_code": "401"}

    class TextBlock:
        type = "text"
        text = "已根据 401 错误码给出排查建议。"

    class ToolClient:
        def __init__(self):
            self.calls = []
            self.responses = [
                type("Response", (), {"content": [ToolUseBlock()]})(),
                type("Response", (), {"content": [TextBlock()]})(),
            ]

        class Messages:
            def __init__(self, owner):
                self.owner = owner

            async def create(self, **kwargs):
                self.owner.calls.append(kwargs)
                return self.owner.responses.pop(0)

        @property
        def messages(self):
            return self.Messages(self)

    client = ToolClient()
    agent = configure_business_tools(TechnicalAgent(client, "test-model"))
    response = asyncio.run(agent.handle(make_request()))

    assert response.success is True
    assert response.tools_used == ["get_error_code_playbook"]
    assert len(response.tool_traces) == 1
    assert response.tool_traces[0]["tool_name"] == "get_error_code_playbook"
    assert response.tool_traces[0]["input"] == {"error_code": "401"}
    assert response.tool_traces[0]["success"] is True
    assert [item["round_no"] for item in response.llm_traces] == [1, 2]
    assert all(item["model"] == "test-model" for item in response.llm_traces)
    assert all(item["status"] == "success" for item in response.llm_traces)
    assert len(client.calls) == 2
    assert {tool["name"] for tool in client.calls[0]["tools"]} == {
        "get_error_code_playbook",
        "query_incident_status",
        "request_human_handoff",
    }
    assert "tool_result" in str(client.calls[1]["messages"])


def test_business_query_tools_use_cache_and_isolate_user_scope():
    class CountingRepository(FakeBusinessRepository):
        def __init__(self):
            self.calls = 0

        async def get_order_status(self, user_id, order_id):
            self.calls += 1
            return {"success": True, "user_id": user_id, "order_id": order_id}

    repository = CountingRepository()
    manager = MCPToolManager(api_key="test-key", model="test-model")
    tool = build_business_tools(repository, manager)["get_order_status"]

    first = asyncio.run(tool.handler(make_request(user_id="u1"), {"order_id": "ORD-DEMO-1001"}))
    second = asyncio.run(tool.handler(make_request(user_id="u1"), {"order_id": "ORD-DEMO-1001"}))
    other_user = asyncio.run(tool.handler(make_request(user_id="u2"), {"order_id": "ORD-DEMO-1001"}))

    assert first["success"] is True
    assert second["cached"] is True
    assert other_user["user_id"] == "u2"
    assert repository.calls == 2


def test_business_tool_failure_degrades_and_handoff_is_never_cached():
    class UnavailableRepository(FakeBusinessRepository):
        async def get_refund_status(self, user_id, order_id):
            raise RuntimeError("mysql unavailable")

        def __init__(self):
            self.ticket_calls = 0

        async def create_handoff_ticket(self, **kwargs):
            self.ticket_calls += 1
            return {"success": True, "ticket_id": f"TKT-{self.ticket_calls}"}

    repository = UnavailableRepository()
    manager = MCPToolManager(api_key="test-key", model="test-model")
    tools = build_business_tools(repository, manager)

    degraded = asyncio.run(tools["get_refund_status"].handler(make_request(), {"order_id": "ORD-DEMO-1002"}))
    first_ticket = asyncio.run(tools["request_human_handoff"].handler(make_request(), {"reason": "需要人工核验"}))
    second_ticket = asyncio.run(tools["request_human_handoff"].handler(make_request(), {"reason": "需要人工核验"}))

    assert degraded["success"] is False
    assert degraded["fallback"] is True
    assert first_ticket["ticket_id"] != second_ticket["ticket_id"]
    assert repository.ticket_calls == 2


def test_textual_handoff_hint_does_not_mark_response_escalated():
    class TextBlock:
        type = "text"
        text = "请先检查 Token；若仍无法解决，可以建议转人工继续排查。"

    agent = TechnicalAgent(
        FakeClient(response=type("Response", (), {"content": [TextBlock()]})()),
        "test-model",
    )

    response = asyncio.run(agent.handle(make_request(message="登录提示 401，请给出排查步骤。")))

    assert response.success is True
    assert response.escalate is False


def test_handoff_function_call_marks_response_escalated_and_records_trace():
    class ToolUseBlock:
        type = "tool_use"
        id = "toolu_handoff"
        name = "request_human_handoff"
        input = {"reason": "需要后台日志进一步核验", "priority": "high"}

    class TextBlock:
        type = "text"
        text = "已记录人工介入申请，请等待后续处理。"

    class ToolClient:
        def __init__(self):
            self.responses = [
                type("Response", (), {"content": [ToolUseBlock()]})(),
                type("Response", (), {"content": [TextBlock()]})(),
            ]

        class Messages:
            def __init__(self, owner):
                self.owner = owner

            async def create(self, **kwargs):
                return self.owner.responses.pop(0)

        @property
        def messages(self):
            return self.Messages(self)

    response = asyncio.run(configure_business_tools(TechnicalAgent(ToolClient(), "test-model")).handle(make_request()))

    assert response.success is True
    assert response.escalate is True
    assert response.tools_used == ["request_human_handoff"]
    assert response.tool_traces[0]["tool_name"] == "request_human_handoff"
    assert response.tool_traces[0]["input"]["reason"] == "需要后台日志进一步核验"


def test_concurrent_requests_keep_agent_traces_isolated():
    class ToolUseBlock:
        type = "tool_use"

        def __init__(self, marker):
            self.id = f"toolu_{marker}"
            self.name = "get_error_code_playbook"
            self.input = {"error_code": marker}

    class TextBlock:
        type = "text"

        def __init__(self, marker):
            self.text = f"{marker} 的排查建议"

    class ConcurrentClient:
        class Messages:
            async def create(inner, **kwargs):
                await asyncio.sleep(0)
                payload = str(kwargs["messages"])
                marker = "401" if "401" in payload else "500"
                if "tool_result" in payload:
                    return type("Response", (), {"content": [TextBlock(marker)]})()
                return type("Response", (), {"content": [ToolUseBlock(marker)]})()

        messages = Messages()

    async def run_both():
        agent = configure_business_tools(TechnicalAgent(ConcurrentClient(), "test-model"))
        return await asyncio.gather(
            agent.handle(make_request(message="登录报 401", entities={"error_code": ["401"]})),
            agent.handle(make_request(message="服务报 500", entities={"error_code": ["500"]})),
        )

    first, second = asyncio.run(run_both())
    assert first.tool_traces[0]["input"] == {"error_code": "401"}
    assert second.tool_traces[0]["input"] == {"error_code": "500"}
    assert len(first.llm_traces) == len(second.llm_traces) == 2


def test_orchestrator_persists_complete_trace_through_repository():
    class RecordingRepository:
        def __init__(self):
            self.traces = []

        async def persist(self, trace):
            self.traces.append(trace)
            return True

    repository = RecordingRepository()
    orchestrator = AgentOrchestrator(api_key="test-key", model="test-model", trace_repository=repository)
    result = OrchestratorResult(
        request_id="trace-unit-check",
        response="已完成排查。",
        agent_type=AgentType.TECHNICAL,
        intent=IntentCategory.TECHNICAL_LOGIN,
        primary_agent=AgentType.TECHNICAL,
        tools_used=["lookup_error_code"],
        tool_traces=[{"tool_name": "lookup_error_code"}],
        llm_traces=[{"agent_type": "technical", "round_no": 1, "model": "test-model"}],
        agent_executions=[{"agent_type": "technical", "status": "success", "success": True, "content_length": 6}],
        composer_content="已完成排查。",
        intent_decision={"final": {"intent": "technical_login", "confidence": 0.9}},
        routing_reason="unit test",
        routing_confidence=0.9,
    )

    asyncio.run(orchestrator._record_tool_trace(result))

    assert repository.traces[0]["request_id"] == "trace-unit-check"
    assert repository.traces[0]["tool_calls"][0]["tool_name"] == "lookup_error_code"
    assert repository.traces[0]["llm_calls"][0]["round_no"] == 1
    assert repository.traces[0]["agent_executions"][0]["content_length"] == 6
    assert repository.traces[0]["composer_content"] == "已完成排查。"
    assert repository.traces[0]["final_response"] == "已完成排查。"
    assert repository.traces[0]["intent_decision"]["final"]["intent"] == "technical_login"
