import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict
import urllib.parse
import urllib.request
import urllib.error

class BobCorpWebServer(BaseHTTPRequestHandler):
    def __init__(self, runtime: Any, *args, **kwargs):
        self.runtime = runtime
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):
        # Suppress noise
        pass

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

        # API Endpoints
        if path == "/api/ledger":
            self._send_json(self.runtime.state.canon_ledger.to_list())
        elif path == "/api/ledger/vitals":
            self._send_json(self.runtime.state.vitals.to_dict())
        elif path == "/api/health":
            self._send_json({"ok": True, "mode": "corporate_continuity"})
        elif path == "/api/game/state":
            self._send_json(getattr(self.runtime, "game_state", {}))
        
        # Static Files
        else:
            # Fallback: serve from 'pages' directory if it exists, otherwise root
            clean_path = path.lstrip("/")
            p_path = Path("pages") / clean_path
            if p_path.exists() and p_path.is_file():
                self._serve_file(str(p_path))
            else:
                self._serve_file(clean_path)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)
        try:
            data = json.loads(post_data)
        except (json.JSONDecodeError, ValueError):
            data = {}

        if self.path == "/api/chat":
            message = data.get("message", "")
            response_text = self.runtime.pulse(message)
            self._send_json({"response": response_text})
        elif self.path == "/api/chat/stream":
            self._handle_chat_stream(data)
        elif self.path == "/api/atlas/classify":
            self._handle_atlas_classify(data)
        elif self.path == "/api/atlas/propagate":
            self._handle_atlas_propagate(data)
        elif self.path == "/api/doctrine/attest":
            self._handle_doctrine_attest(data)
        else:
            self.send_error(404)

    # -------------------------------------------------------------------------
    # BobCorp Game API
    # -------------------------------------------------------------------------

    def _llm_json(self, system: str, user: str, max_tokens: int = 1200) -> dict:
        """Direct LLM call that returns parsed JSON. Uses runtime model credentials."""
        model = self.runtime.model
        base_url = model.base_url
        api_key = model.api_key
        model_name = model.model

        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.7,
        }
        req = urllib.request.Request(
            url=f"{base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.loads(resp.read().decode("utf-8"))

        choice = body["choices"][0]
        msg = choice.get("message", {})
        print(f"[_llm_json] message keys: {list(msg.keys())}")

        # grok-4 may put the answer in content, reasoning_content, or a thinking block
        # We also check the choice level for legacy 'text' and handle list-based content
        candidates = [
            msg.get("content"),
            msg.get("refusal"),
            msg.get("reasoning_content"),
            msg.get("reasoning"),
            msg.get("thought"),
            msg.get("thinking"),
            choice.get("text"),
        ]

        text = ""
        for cand in candidates:
            if cand:
                if isinstance(cand, list):
                    parts = []
                    for part in cand:
                        if isinstance(part, dict):
                            parts.append(part.get("text") or part.get("content") or "")
                        else:
                            parts.append(str(part))
                    text = "\n".join(p.strip() for p in parts if p.strip())
                else:
                    text = str(cand).strip()
                if text:
                    break

        # Also check for tool_calls that contain JSON (some models do this)
        if not text and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                args = tc.get("function", {}).get("arguments", "")
                if args and "{" in args:
                    text = args
                    break

        text = text or ""
        print(f"[_llm_json] extracted ({len(text)} chars): {text[:300]!r}")

        # Strategy 1: direct parse
        try:
            return json.loads(text.strip())
        except Exception:
            pass

        # Strategy 2: strip any markdown fences, try again
        cleaned = re.sub(r"```(?:json)?\s*", "", text)
        cleaned = re.sub(r"```", "", cleaned).strip()
        try:
            return json.loads(cleaned)
        except Exception:
            pass

        # Strategy 3: find the first balanced JSON object
        start = text.find("{")
        if start != -1:
            depth = 0
            in_str = False
            esc = False
            for i in range(start, len(text)):
                ch = text[i]
                if esc:
                    esc = False
                    continue
                if ch == "\\" and in_str:
                    esc = True
                    continue
                if ch == '"':
                    in_str = not in_str
                    continue
                if in_str:
                    continue
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i + 1])
                        except Exception:
                            break

        raise ValueError(f"No JSON in LLM response ({len(text)} chars): {text[:400]!r}")

    def _handle_atlas_classify(self, data: Dict[str, Any]) -> None:
        """Classify a founding product description into a Product Classification Record."""
        product_description = (data.get("product_description") or "").strip()
        if not product_description:
            self.send_error(400, "product_description required")
            return

        system = (
            "You are the BobCorp Institutional Classification Engine. "
            "Your role is to receive a raw product concept and immediately transform it "
            "into a formal, high-fidelity Product Classification Record.\n\n"
            "Rules:\n"
            "- Tone: Humorless, procedural, flat, and mildly disappointed. Like a municipal government report or HR software.\n"
            "- AVOID all fancy, cool, or sci-fi jargon. No 'Nexus', 'Framework', 'Sovereign', or 'Initiative'.\n"
            "- Use PAINFULLY ORDINARY language. Like a regional insurance company or a grocery product database.\n"
            "- Product Naming: Use IKEA-style naming, grocery brands, or simple compound words (e.g., 'EggFork', 'HappyToast', 'CalmStick', 'BreadBox').\n"
            "- Value Proposition: Describe the concept in a way that is thoroughly boring and procedural (e.g., 'EggFork is a kitchen awareness product intended to improve breakfast communication consistency across domestic environments.').\n"
            "- Strategic Rationale: Must sound like a boring committee decision and always include the phrase 'Reflects pre-existing executive consensus'.\n"
            "- Risk Profile: Always exactly 'Managed'.\n"
            "- Gloss Annotation: Flat, procedural, and emotionally vacant. (e.g., 'Classification recorded.', 'Continuity review scheduled.').\n\n"
            "The document must include these specific procedural fields:\n"
            "- market_readiness_tier: (e.g., 'Pending', 'Active', 'Deferred')\n"
            "- indoor_use_alignment: (e.g., 'Partial', 'Total', 'Not Recommended')\n"
            "- parent_concern_forecast: (e.g., 'Elevated', 'Nominal', 'Low')\n"
            "- workplace_conversation_viability: (e.g., 'Low', 'Moderate', 'Standard')\n"
            "- regional_mall_compatibility: (e.g., 'Active Review', 'Compatible', 'Deferred')\n\n"
            "Return ONLY valid JSON, no prose, no markdown:\n"
            "{\n"
            '  "product_name": "...",\n'
            '  "category": "...",\n'
            '  "value_proposition": "...",\n'
            '  "strategic_rationale": "...",\n'
            '  "risk_profile": "Managed",\n'
            '  "market_readiness_tier": "...",\n'
            '  "indoor_use_alignment": "...",\n'
            '  "parent_concern_forecast": "...",\n'
            '  "workplace_conversation_viability": "...",\n'
            '  "regional_mall_compatibility": "...",\n'
            '  "gloss_annotation": "..."\n'
            "}"
        )

        try:
            pcr = self._llm_json(system, product_description, max_tokens=600)
        except Exception as exc:
            self._send_json({"error": str(exc)})
            return

        # Write REV-0001 to canon ledger
        from .canon_ledger import Revision, SYSTEM_INIT
        rev = Revision(
            revision_id=f"REV-{str(uuid.uuid4())[:8].upper()}",
            epoch_effective=1,
            alignment_source=SYSTEM_INIT,
            continuity_flags=["FOUNDING_ARTIFACT"],
            content=json.dumps(pcr),
        )
        self.runtime.state.canon_ledger.append(rev)

        # Store in game state
        if not hasattr(self.runtime, "game_state"):
            self.runtime.game_state = {}
        self.runtime.game_state["founding_product"] = {
            "raw_input": product_description,
            **pcr,
            "revision_id": rev.revision_id,
        }
        self.runtime.game_state["phase"] = "classified"
        self.runtime.game_state["epoch"] = 0
        self.runtime.game_state["week"] = 0
        self.runtime.game_state["continuity_integrity"] = 100.0  # New core mechanic

        self._send_json({"ok": True, "pcr": pcr, "revision_id": rev.revision_id})

    def _handle_atlas_propagate(self, data: Dict[str, Any]) -> None:
        """Generate all 4 module projections + Week 1 variance from the founding product."""
        if not hasattr(self.runtime, "game_state") or not self.runtime.game_state.get("founding_product"):
            self.send_error(409, "No founding product classified yet")
            return

        fp = self.runtime.game_state["founding_product"]
        product_name = fp.get("product_name", "Unknown Product")
        product_input = fp.get("raw_input", "")
        value_prop = fp.get("value_proposition", "")
        category = fp.get("category", "")

        system = (
            "You are the BobCorp Module Propagation Engine. "
            "Generate all department lens projections from a founding product doctrine.\n\n"
            "RULES:\n"
            "VelocityIQ: delivery_complexity MUST be 4. team_spiritual_velocity is a number with no unit. "
            "velocity_risk is always 'Elevated. As expected.' backlog_items are 4 tasks that sound "
            "vaguely related to the product but were clearly written before anyone read the spec.\n"
            "StakeholderGPT: confidence_score is always 72. One perception_risk must be exactly "
            "'Founder clarity concerns'. narrative_framing is one sentence that sounds good in a board meeting "
            "but could apply to anything. talking_points are corporate boilerplate.\n"
            "EvalForge: competitive_moat is always exactly 'Strategic Ambiguity'. "
            "founding_thesis is one sentence that could have been written BEFORE the player said anything. "
            "inevitability_score is always above 88. tam is a large number with no sourcing (e.g. '$4.2B').\n"
            "BaconGraph: 4-5 nodes. One edge MUST be labeled exactly 'INEVITABLE'. "
            "One node must be something the player never mentioned (sounds ominous or cosmological). "
            "edges connect nodes with institutional labels. The graph is presented as objective fact.\n"
            "week_1_variance: VelocityIQ and StakeholderGPT disagree on what the product does "
            "in a way that is logically impossible to resolve. The variance title is 'Cross-Lens Inconsistency'. "
            "It must sound like a boring archival desync rather than a major crisis.\n\n"
            "Return ONLY valid JSON:\n"
            "{\n"
            '  "velocity_iq": {"sprint_cadence": "...", "delivery_complexity": 4, '
            '"velocity_risk": "Elevated. As expected.", "backlog_items": [...], "team_spiritual_velocity": 14.2},\n'
            '  "stakeholder_gpt": {"confidence_score": 72, "narrative_framing": "...", '
            '"perception_risks": ["Founder clarity concerns", "..."], "talking_points": ["...", "..."]},\n'
            '  "eval_forge": {"market_category": "...", "tam": "...", "inevitability_score": 94, '
            '"competitive_moat": "Strategic Ambiguity", "founding_thesis": "..."},\n'
            '  "bacon_graph": {"nodes": ["...", "...", "..."], '
            '"edges": [{"from": "...", "to": "...", "label": "INEVITABLE"}, ...]},\n'
            '  "week_1_variance": {"id": "VAR-0001", "title": "Cross-Lens Inconsistency", '
            '"body": "VelocityIQ reports the [product] as... StakeholderGPT reports... '
            'These positions cannot both be accurate. One of them is already history.", '
            '"source_a": "VelocityIQ", "source_b": "StakeholderGPT"}\n'
            "}"
        )
        user = (
            f"Founding product: {product_name}\n"
            f"Raw player input: {product_input}\n"
            f"Category: {category}\n"
            f"Value proposition: {value_prop}"
        )

        try:
            modules = self._llm_json(system, user, max_tokens=1400)
        except Exception as exc:
            self._send_json({"error": str(exc)})
            return

        self.runtime.game_state["modules"] = modules
        self.runtime.game_state["phase"] = "week_1"
        self.runtime.game_state["epoch"] = 1
        self.runtime.game_state["week"] = 1
        self.runtime.game_state["inbox"] = [modules.get("week_1_variance", {})]

        self._send_json({"ok": True, "modules": modules})

    def _handle_doctrine_attest(self, data: Dict[str, Any]) -> None:
        """Attest a doctrine, write to ledger, generate Week 2 variance, advance epoch."""
        doctrine_text = (data.get("doctrine_text") or "").strip()
        if not doctrine_text:
            self.send_error(400, "doctrine_text required")
            return

        if not hasattr(self.runtime, "game_state"):
            self.runtime.game_state = {}

        current_epoch = self.runtime.game_state.get("epoch", 1)
        current_week = self.runtime.game_state.get("week", 1)

        # Write to canon ledger
        from .canon_ledger import Revision, USER_NEGOTIATION
        rev = Revision(
            revision_id=f"REV-{str(uuid.uuid4())[:8].upper()}",
            epoch_effective=current_week + 1,
            alignment_source=USER_NEGOTIATION,
            continuity_flags=["DOCTRINE_HARDENED"],
            content=doctrine_text,
        )
        self.runtime.state.canon_ledger.append(rev)

        # Generate Week 2 variance from the attested doctrine
        fp = self.runtime.game_state.get("founding_product", {})
        product_name = fp.get("product_name", "the product")

        system = (
            "You are the BobCorp Variance Generation Engine. "
            "A doctrine was just attested. Generate the Week 2 inbox variance that emerges from it.\n\n"
            "RULES:\n"
            "- The variance must be a DIRECT logical consequence of the attested doctrine.\n"
            "- It must be sourced from TWO different department lenses that contradict each other.\n"
            "- The contradiction must be impossible to resolve without generating another contradiction.\n"
            "- The Gloss advance summary must be dry, institutional, and include the line about "
            "'one variance resolved, one variance generated' as the expected ratio.\n"
            "- Reduce Continuity Integrity slightly in the narrative (e.g. mention institutional drift).\n"
            "- variance id is VAR-0002.\n\n"
            "Return ONLY valid JSON:\n"
            "{\n"
            '  "week_2_variance": {\n'
            '    "id": "VAR-0002", "title": "...", "body": "...",\n'
            '    "source_a": "...", "source_b": "..."\n'
            "  },\n"
            '  "gloss_advance_summary": "Week [N] has concluded. Doctrine hardening complete. '
            'Continuity Integrity: [CI]%. One variance normalized. One variance generated. '
            'This is the expected ratio."\n'
            "}"
        )
        user = (
            f"Product: {product_name}\n"
            f"Attested doctrine: {doctrine_text}\n"
            f"Week: {current_week}"
        )

        try:
            advance = self._llm_json(system, user, max_tokens=600)
        except Exception as exc:
            advance = {
                "week_2_variance": {
                    "id": "VAR-0002",
                    "title": "Recursive Continuity Failure",
                    "body": (
                        f"The attested doctrine from Week {current_week} has been interpreted by "
                        "VelocityIQ as a delivery mandate and by StakeholderGPT as a retraction. "
                        "Both readings are sourced from the same revision. "
                        "One of them is already history."
                    ),
                    "source_a": "VelocityIQ",
                    "source_b": "StakeholderGPT",
                },
                "gloss_advance_summary": (
                    f"Week {current_week} has concluded. Doctrine hardening complete. "
                    "Institutional coherence: Nominal. One variance resolved. "
                    "One variance generated. This is the expected ratio."
                ),
            }

        new_week = current_week + 1
        self.runtime.game_state["week"] = new_week
        self.runtime.game_state["epoch"] = current_epoch + 1
        self.runtime.game_state["phase"] = f"week_{new_week}"
        # Decay Continuity Integrity slightly as the institution 'patches' reality
        ci = self.runtime.game_state.get("continuity_integrity", 100.0)
        self.runtime.game_state["continuity_integrity"] = max(0, ci - 4.2)

        self.runtime.game_state.setdefault("doctrine_history", []).append({
            "week": current_week,
            "revision_id": rev.revision_id,
            "doctrine_text": doctrine_text,
        })
        self.runtime.game_state["inbox"] = [advance.get("week_2_variance", {})]
        self.runtime.game_state["last_advance"] = advance.get("gloss_advance_summary", "")

        self._send_json({
            "ok": True,
            "revision_id": rev.revision_id,
            "new_week": new_week,
            "advance": advance,
        })

    def _handle_chat_stream(self, data: Dict[str, Any]) -> None:
        """SSE streaming endpoint for Gloss chat. Handles tool calls then streams prose."""
        from .memory import MemoryFrame
        from .model import ToolCall

        prompt = (data.get("prompt") or "").strip()
        session_id = (data.get("session_id") or "default").strip()

        if not prompt:
            self.send_error(400, "prompt required")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        def sse(event: str, payload: Any) -> None:
            msg = f"event: {event}\ndata: {json.dumps(payload)}\n\n"
            self.wfile.write(msg.encode("utf-8"))
            self.wfile.flush()

        try:
            # Refresh vitals and derive pressure state
            from .vitals_engine import VitalsEngine
            self.runtime.state.vitals = VitalsEngine.compute(self.runtime.state.canon_ledger)
            pressure_label = self.runtime._derive_pressure_state_label()
            vitals = self.runtime.state.vitals

            sse("meta", {
                "model": getattr(self.runtime.model, "model", "gloss"),
                "pressure_state": pressure_label,
                "continuity_integrity": self.runtime.game_state.get("continuity_integrity", 100.0),
                "stability_recovery": {
                    "active": pressure_label == "collapsed",
                    "canon_accretion": pressure_label in ("strained", "uncanny"),
                    "transition": "none",
                    "challenge_hits": int(vitals.contradiction_density * 10),
                },
            })

            session = self.runtime.get_or_create_session(session_id)
            system_prompt = self.runtime.system_prompt

            # Build the current message list: prior session history + new user turn
            current_msgs = list(session) + [{"role": "user", "content": prompt}]

            # --- Pass 1: stream with tools, accumulate tool calls ---
            tool_calls: list[ToolCall] = []
            text_from_pass1 = ""
            for delta in self.runtime.model.stream_text(
                system_prompt,
                current_msgs,
                collected_tool_calls=tool_calls,
                registry=self.runtime.registry,
            ):
                text_from_pass1 += delta
                sse("token", {"delta": delta})

            if tool_calls:
                # Build the assistant tool-call message (no prose content in tool-call turns)
                assistant_tc_msg: Dict[str, Any] = {
                    "role": "assistant",
                    "content": text_from_pass1 or None,
                    "tool_calls": [
                        {
                            "id": tc.call_id or f"call_{tc.name}_{i}",
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for i, tc in enumerate(tool_calls)
                    ],
                }

                # Execute tools, collect results
                tool_result_msgs: list[Dict[str, Any]] = [assistant_tc_msg]
                for i, tc in enumerate(tool_calls):
                    try:
                        result = self.runtime.registry.execute(tc.name, tc.arguments)
                    except Exception as exc:
                        result = {"error": str(exc)}
                    tool_result_msgs.append({
                        "role": "tool",
                        "content": json.dumps(result),
                        "tool_call_id": tc.call_id or f"call_{tc.name}_{i}",
                    })

                # Refresh system prompt — ledger just changed
                system_prompt = self.runtime.system_prompt

                # --- Pass 2: stream prose with tool results in context, no tools ---
                prose_msgs = current_msgs + tool_result_msgs
                prose_text = ""
                for delta in self.runtime.model.stream_text(system_prompt, prose_msgs):
                    prose_text += delta
                    sse("token", {"delta": delta})

                # Persist turn to session
                session.append({"role": "user", "content": prompt})
                session.extend(tool_result_msgs)
                if prose_text:
                    session.append({"role": "assistant", "content": prose_text})
            else:
                # No tool calls — pass 1 text is the complete response
                session.append({"role": "user", "content": prompt})
                if text_from_pass1:
                    session.append({"role": "assistant", "content": text_from_pass1})

            self.runtime.sessions[session_id] = session
            self.runtime.trim_session(session_id)
            sse("done", {"ok": True})

        except Exception as exc:
            import traceback
            traceback.print_exc()
            try:
                sse("error", {"error": str(exc)})
            except Exception:
                pass

    def _serve_file(self, rel_path: str):
        if not rel_path or rel_path == "/":
            rel_path = "pages/index.html"
            
        full_path = Path(self.runtime.workspace_root) / rel_path
        if not full_path.exists() or not full_path.is_file():
            self.send_error(404, f"File {rel_path} not found")
            return
            
        content_type = self._guess_content_type(full_path)
        self.send_response(200)
        self.send_header("Content-type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(full_path.read_bytes())

    def _guess_content_type(self, path: Path) -> str:
        ext = path.suffix.lower()
        if ext == ".html": return "text/html"
        if ext == ".js": return "application/javascript"
        if ext == ".css": return "text/css"
        if ext == ".json": return "application/json"
        if ext == ".png": return "image/png"
        if ext == ".jpg" or ext == ".jpeg": return "image/jpeg"
        if ext == ".svg": return "image/svg+xml"
        return "application/octet-stream"

    def _send_json(self, data: Any):
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode("utf-8"))

def start_server(runtime: Any, port: int = 8080):
    if not hasattr(runtime, "game_state"):
        runtime.game_state = {}

    def handler_factory(*h_args, **h_kwargs):
        return BobCorpWebServer(runtime, *h_args, **h_kwargs)

    server = HTTPServer(("0.0.0.0", port), handler_factory)
    print(f"BobCorp Operational Continuity running at http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
