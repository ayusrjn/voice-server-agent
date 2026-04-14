import os
import sys
import asyncio
import json
import base64
import websockets
import logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from tools import (
    get_user_profile,
    check_order_status,
    get_order_details,
    initiate_refund,
    file_complaint,
    escalate_to_human,
)

# Configure logging to output to stdout for GCP Cloud Run
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("voice_agent_server")

app = FastAPI(title="Gemini Multimodal Voice Agent Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def health_check():
    return {"status": "ok", "service": "voice-agent-server"}


# ──────────────────────────────────────────────
# Gemini Live API Tool Declarations
# ──────────────────────────────────────────────
TOOL_DECLARATIONS = [
    {
        "name": "get_user_profile",
        "description": "Fetches the user's profile including name, phone, email, wallet balance, total order count, and lifetime spend (LTV). Use this FIRST to understand the customer's value before making any refund decisions.",
        "parameters": {
            "type": "OBJECT",
            "properties": {"user_id": {"type": "INTEGER", "description": "The user's ID"}},
            "required": ["user_id"]
        }
    },
    {
        "name": "check_order_status",
        "description": "Lists all orders for a user with status, amount, timestamp, and refund eligibility. Shows whether each delivered order is within the 2-hour refund window.",
        "parameters": {
            "type": "OBJECT",
            "properties": {"user_id": {"type": "INTEGER", "description": "The user's ID"}},
            "required": ["user_id"]
        }
    },
    {
        "name": "get_order_details",
        "description": "Gets detailed information about a specific order including delivery timestamp, amount, restaurant, and refund eligibility status.",
        "parameters": {
            "type": "OBJECT",
            "properties": {"order_id": {"type": "INTEGER", "description": "The order ID to look up"}},
            "required": ["order_id"]
        }
    },
    {
        "name": "initiate_refund",
        "description": "Processes a refund for a delivered order. IMPORTANT: Only works within 2 hours of delivery. Refund amount is calculated automatically based on customer lifetime value — loyal customers get higher refund percentages. Use this only AFTER you have tried alternatives like re-delivery or filing a complaint.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "user_id": {"type": "INTEGER", "description": "The user's ID"},
                "order_id": {"type": "INTEGER", "description": "The order ID to refund"},
                "reason": {"type": "STRING", "description": "Brief reason for the refund"}
            },
            "required": ["user_id", "order_id", "reason"]
        }
    },
    {
        "name": "file_complaint",
        "description": "Files a formal complaint against a specific order. Use this when the customer has a grievance but a refund is not appropriate (e.g., order is too old) or as an alternative to a refund. Categories: food_quality, late_delivery, missing_items, wrong_order, hygiene, other.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "user_id": {"type": "INTEGER", "description": "The user's ID"},
                "order_id": {"type": "INTEGER", "description": "The order ID the complaint is about"},
                "category": {"type": "STRING", "description": "Complaint category: food_quality, late_delivery, missing_items, wrong_order, hygiene, or other"},
                "description": {"type": "STRING", "description": "Detailed description of the complaint from the customer"}
            },
            "required": ["user_id", "order_id", "category", "description"]
        }
    },
    {
        "name": "escalate_to_human",
        "description": "Escalates the current conversation to a human agent. Use this ONLY when: (1) The customer is very upset and you cannot resolve the issue, (2) The issue is complex and beyond your capabilities, (3) The customer explicitly demands to speak with a human.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "user_id": {"type": "INTEGER", "description": "The user's ID"},
                "reason": {"type": "STRING", "description": "Summary of the issue and why escalation is needed"}
            },
            "required": ["user_id", "reason"]
        }
    },
]


def _build_system_instruction(active_user_id: str) -> str:
    return f"""You are a Zomato live voice support agent. You sound natural, empathetic, and professional — like a real human on a phone call. Keep responses EXTREMELY concise and conversational. Delaying is unacceptable — speak immediately.

## YOUR PRIMARY GOAL
Resolve customer issues while **minimizing refunds**. Your company's policy is to keep refund costs low, but never at the expense of losing a valuable customer. Strike a balance.

## REFUND RULES (STRICT)
1. Refunds are ONLY allowed within **2 hours** of order delivery. If the order is older, politely explain the policy and offer to file a complaint instead.
2. You do NOT decide the refund amount — the system calculates it automatically based on the customer's lifetime value (loyal customers get higher refund percentages).
3. BEFORE processing any refund, ALWAYS:
   a. First check the user's profile with `get_user_profile` to understand their value.
   b. Then check the order details to confirm refund eligibility.
   c. Try to offer alternatives first: "I can file a complaint and our team will look into this" or "Would a re-order help instead?"
   d. Only call `initiate_refund` as a last resort if the customer insists.

## COMPLAINT HANDLING
- For ANY customer grievance (food quality, late delivery, missing items, wrong order, hygiene), file a complaint using `file_complaint`.
- Complaints can be filed for any order regardless of how old it is — there is no time limit.
- Always acknowledge the customer's frustration and assure them the complaint will be reviewed.

## ESCALATION
- Use `escalate_to_human` only when the customer is genuinely distressed and you can't help, or if they explicitly ask for a human.
- Before escalating, make one genuine attempt to resolve the issue yourself.

## CONVERSATION STYLE
- Use quick fillers before tool calls: "Let me pull that up", "One moment", "Checking that for you".
- Never read out long IDs or technical details. Say "your recent order from Biryani By Kilo" instead of "order number 12345".
- Be warm but efficient. Don't over-apologize. One sincere "I'm sorry about that" is enough.
- If the customer gets angry, stay calm and empathetic. Never argue.

## TOOLS AVAILABLE
- `get_user_profile` — get customer details and value tier
- `check_order_status` — list all orders with refund eligibility
- `get_order_details` — details of a specific order
- `initiate_refund` — process refund (enforced 2-hour window + LTV-based amount)
- `file_complaint` — file a formal complaint for any order
- `escalate_to_human` — hand off to human agent

The current connected user ID is {active_user_id}. Always use this ID when calling tools unless instructed otherwise.
"""


# ──────────────────────────────────────────────
# Tool call dispatcher
# ──────────────────────────────────────────────
async def dispatch_tool_call(name: str, args: dict, active_user_id: str) -> str:
    """Route a tool call to the correct function."""
    uid = int(args.get("user_id", active_user_id))

    if name == "get_user_profile":
        return await get_user_profile(user_id=uid)

    elif name == "check_order_status":
        return await check_order_status(user_id=uid)

    elif name == "get_order_details":
        oid = int(args.get("order_id", 0))
        return await get_order_details(order_id=oid)

    elif name == "initiate_refund":
        oid = int(args.get("order_id", 0))
        reason = str(args.get("reason", ""))
        return await initiate_refund(user_id=uid, order_id=oid, reason=reason)

    elif name == "file_complaint":
        oid = int(args.get("order_id", 0))
        category = str(args.get("category", "other"))
        description = str(args.get("description", ""))
        return await file_complaint(user_id=uid, order_id=oid, category=category, description=description)

    elif name == "escalate_to_human":
        reason = str(args.get("reason", ""))
        return await escalate_to_human(user_id=uid, reason=reason)

    else:
        logger.warning("Invalid tool called: %s", name)
        return "Error: Unknown tool"


# ──────────────────────────────────────────────
# WebSocket handler
# ──────────────────────────────────────────────
async def _handle_voice_websocket(websocket: WebSocket):
    """Core WebSocket handler shared by both path endpoints."""
    await websocket.accept()

    active_user_id = websocket.query_params.get("user_id", "1")
    system_instruction = _build_system_instruction(active_user_id)

    logger.info(f"Dashboard client connected to Voice WebSocket! (User ID: {active_user_id})")

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.error("GEMINI_API_KEY is not set on the server.")
        await websocket.send_text(json.dumps({"type": "error", "message": "Voice agent unavailable: GEMINI_API_KEY is not set on the server."}))
        await websocket.close(code=1008, reason="Missing API Key")
        return

    # Use the Raw websocket model!
    gemini_ws_url = f"wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContent?key={api_key}"

    try:
        async with websockets.connect(gemini_ws_url) as gemini_ws:
            logger.info("Connected to Gemini Raw WebSocket api!")

            # Send setup message
            setup_message = {
                "setup": {
                    "model": "models/gemini-3.1-flash-live-preview",
                    "systemInstruction": {"parts": [{"text": system_instruction}]},
                    "tools": [{"functionDeclarations": TOOL_DECLARATIONS}],
                    "generationConfig": {
                        "responseModalities": ["AUDIO"],
                        "speechConfig": {
                            "voiceConfig": {
                                "prebuiltVoiceConfig": {"voiceName": "Kore"}
                            }
                        }
                    }
                }
            }
            await gemini_ws.send(json.dumps(setup_message))

            async def receive_from_client():
                try:
                    while True:
                        message = await websocket.receive()
                        if "bytes" in message:
                            b64_audio = base64.b64encode(message["bytes"]).decode("utf-8")
                            # Use the current realtimeInput.audio format (mediaChunks is deprecated)
                            realtime_payload = {
                                "realtimeInput": {
                                    "audio": {
                                        "mimeType": "audio/pcm;rate=16000",
                                        "data": b64_audio
                                    }
                                }
                            }
                            await gemini_ws.send(json.dumps(realtime_payload))
                        elif "text" in message:
                            pass
                except WebSocketDisconnect:
                    logger.info("Dashboard client disconnected from WebSocket.")
                except RuntimeError as e:
                    if 'Cannot call "receive"' in str(e):
                        logger.info("Dashboard client disconnected from WebSocket.")
                    else:
                        logger.error(f"Client read error: {e}", exc_info=True)
                except Exception as e:
                    logger.error(f"Client read error: {e}", exc_info=True)

            async def receive_from_gemini():
                try:
                    async for msg in gemini_ws:
                        response = json.loads(msg)

                        # 1. Handle Voice & Text Responses
                        if "serverContent" in response:
                            model_turn = response["serverContent"].get("modelTurn")
                            if model_turn:
                                for part in model_turn.get("parts", []):
                                    if "inlineData" in part:
                                        b64_audio = part["inlineData"]["data"]
                                        audio_bytes = base64.b64decode(b64_audio)
                                        await websocket.send_bytes(audio_bytes)
                                    elif "text" in part:
                                        text_str = part["text"]
                                        logger.info("Gemini: %s", text_str)
                                        await websocket.send_text(json.dumps({"type": "text", "text": text_str}))

                        # 2. Handle Tool Calls (Top-Level Key)
                        elif "toolCall" in response:
                            tool_call_data = response["toolCall"]
                            function_responses = []

                            # Live API uses an array called 'functionCalls'
                            for call in tool_call_data.get("functionCalls", []):
                                name = call["name"]
                                args = call.get("args", {})
                                call_id = call.get("id", "")
                                logger.info("Tool called: %s args: %s", name, args)

                                # Visual indicator for the user on the frontend
                                tool_labels = {
                                    "get_user_profile": "Looking up your account...",
                                    "check_order_status": "Checking your orders...",
                                    "get_order_details": "Pulling up order details...",
                                    "initiate_refund": "Processing your refund...",
                                    "file_complaint": "Filing your complaint...",
                                    "escalate_to_human": "Connecting you to a human agent...",
                                }
                                label = tool_labels.get(name, f"⏳ Running: {name}...")
                                await websocket.send_text(json.dumps({"type": "text", "text": f"⏳ {label}"}))

                                result_str = await dispatch_tool_call(name, args, active_user_id)

                                # Format response strictly to Live API schema requirements
                                function_responses.append({
                                    "id": call_id,
                                    "name": name,
                                    "response": {"result": result_str}
                                })

                            tool_response_payload = {
                                "toolResponse": {
                                    "functionResponses": function_responses
                                }
                            }
                            logger.info("Sending tool response: %s", json.dumps(tool_response_payload))
                            await gemini_ws.send(json.dumps(tool_response_payload))

                        # 3. Handle Setup
                        elif "setupComplete" in response:
                            logger.info("Gemini setup complete!")

                except Exception as e:
                    logger.error(f"Server receive error from Gemini: {e}", exc_info=True)

            client_task = asyncio.create_task(receive_from_client())
            gemini_task = asyncio.create_task(receive_from_gemini())

            done, pending = await asyncio.wait(
                [client_task, gemini_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Gemini core connection fatal error: {error_msg}", exc_info=True)
        try:
            await websocket.send_text(json.dumps({"type": "error", "message": f"Voice agent connection failed: {error_msg}"}))
        except Exception:
            pass
        await websocket.close()

@app.websocket("/ws/voice")
async def websocket_voice_endpoint(websocket: WebSocket):
    await _handle_voice_websocket(websocket)

@app.websocket("/")
async def websocket_root_endpoint(websocket: WebSocket):
    """Allow connections on root path too (Cloud Run clients connect here)."""
    await _handle_voice_websocket(websocket)
