import os
import sys
import asyncio
import json
import base64
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from tools import check_order_status, initiate_refund

app = FastAPI(title="Gemini Multimodal Voice Agent Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Removed static SYSTEM_INSTRUCTION
@app.websocket("/ws/voice")
async def websocket_voice_endpoint(websocket: WebSocket):
    await websocket.accept()
    
    active_user_id = websocket.query_params.get("user_id", "1")
    system_instruction = f"""
You are a helpful Zomato live voice support agent. Keep your responses EXTREMELY brief, hyper-concise, and conversational. Act exactly like a real human on a fast phone call. Delaying a response is unacceptable, speak immediately.
Use tools `check_order_status` and `initiate_refund` when needed. 
CRITICAL: Before looking up a database using tools, ALWAYS say a very quick filler like "Hmm, let me check", or "Pulling that up", so the user knows you heard them. 
Do not read out long ids, just mention what's important. The current connected user ID is {active_user_id}.
"""
    
    print(f"Dashboard client connected to Voice WebSocket! (User ID: {active_user_id})")
    
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        await websocket.send_text(json.dumps({"type": "error", "message": "Voice agent unavailable: GEMINI_API_KEY is not set on the server."}))
        await websocket.close(code=1008, reason="Missing API Key")
        return

    # Use the Raw websocket model!
    gemini_ws_url = f"wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContent?key={api_key}"

    try:
        async with websockets.connect(gemini_ws_url) as gemini_ws:
            print("Connected to Gemini Raw WebSocket api!")
            
            # Send setup message
            setup_message = {
                "setup": {
                    "model": "models/gemini-3.1-flash-live-preview",
                    "systemInstruction": {"parts": [{"text": system_instruction}]},
                    "tools": [{
                        "functionDeclarations": [
                            {
                                "name": "check_order_status",
                                "description": "Checks the active and historical order status for a given user.",
                                "parameters": {
                                    "type": "OBJECT",
                                    "properties": {"user_id": {"type": "INTEGER"}},
                                    "required": ["user_id"]
                                }
                            },
                            {
                                "name": "initiate_refund",
                                "description": "Initiates a refund for a user's order and logs the complaint transcript.",
                                "parameters": {
                                    "type": "OBJECT",
                                    "properties": {
                                        "user_id": {"type": "INTEGER"},
                                        "order_id": {"type": "INTEGER"},
                                        "reason": {"type": "STRING"}
                                    },
                                    "required": ["user_id", "order_id", "reason"]
                                }
                            }
                        ]
                    }],
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
                            # 1007 protocol format via raw WS payload
                            b64_audio = base64.b64encode(message["bytes"]).decode("utf-8")
                            realtime_payload = {
                                "clientContent": {
                                    "turns": [{
                                        "role": "user",
                                        "parts": [{
                                            "inlineData": {
                                                "mimeType": "audio/pcm;rate=16000",
                                                "data": b64_audio
                                            }
                                        }]
                                    }],
                                    "turnComplete": True
                                }
                            }
                            # The Gemini Live API also supports realtimeInput:
                            realtime_payload = {
                                "realtimeInput": {
                                    "mediaChunks": [{
                                        "mimeType": "audio/pcm;rate=16000",
                                        "data": b64_audio
                                    }]
                                }
                            }
                            await gemini_ws.send(json.dumps(realtime_payload))
                        elif "text" in message:
                            pass
                except WebSocketDisconnect:
                    print("Dashboard client disconnected from WebSocket.")
                except RuntimeError as e:
                    if 'Cannot call "receive"' in str(e):
                        print("Dashboard client disconnected from WebSocket.")
                    else:
                        print(f"Client read error: {e}")
                except Exception as e:
                    print(f"Client read error: {e}")
            
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
                                        print("Gemini:", text_str)
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
                                print(f"Tool called: {name} args: {args}")
                                
                                # Visual indicator for the user on the frontend
                                await websocket.send_text(json.dumps({"type": "text", "text": f"⏳ Checking system for: {name}..."}))
                                
                                result_str = ""
                                if name == "check_order_status":
                                    uid = args.get("user_id", active_user_id)
                                    result_str = await check_order_status(user_id=int(uid))
                                elif name == "initiate_refund":
                                    uid = args.get("user_id", active_user_id)
                                    oid = args.get("order_id", 0)
                                    reason_str = args.get("reason", "")
                                    result_str = await initiate_refund(
                                        user_id=int(uid),
                                        order_id=int(oid),
                                        reason=str(reason_str)
                                    )
                                else:
                                    result_str = "Error: Invalid tool"
                                    
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
                            print("Sending tool response:", tool_response_payload)
                            await gemini_ws.send(json.dumps(tool_response_payload))
                            
                        # 3. Handle Setup
                        elif "setupComplete" in response:
                            print("Gemini setup complete!")
                            
                except Exception as e:
                    print(f"Server receive error from Gemini: {e}")

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
        print(f"Gemini core connection fatal error: {error_msg}")
        try:
            await websocket.send_text(json.dumps({"type": "error", "message": f"Voice agent connection failed: {error_msg}"}))
        except Exception:
            pass
        await websocket.close()
