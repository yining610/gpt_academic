"""
Tests:

- custom_path false / no user auth:
    -- upload file(yes)
    -- download file(yes)
    -- websocket(yes)
    -- block __pycache__ access(yes)
        -- rel (yes)
        -- abs (yes)
    -- block user access(fail) http://localhost:45013/file=gpt_log/admin/chat_secrets.log
        -- fix(commit f6bf05048c08f5cd84593f7fdc01e64dec1f584a)-> block successful

- custom_path yes("/cc/gptac") / no user auth:
    -- upload file(yes)
    -- download file(yes)
    -- websocket(yes)
    -- block __pycache__ access(yes)
    -- block user access(yes)

- custom_path yes("/cc/gptac/") / no user auth:
    -- upload file(yes)
    -- download file(yes)
    -- websocket(yes)
    -- block user access(yes)

- custom_path yes("/cc/gptac/") / + user auth:
    -- upload file(yes)
    -- download file(yes)
    -- websocket(yes)
    -- block user access(yes)
    -- block user-wise access (yes)

- custom_path no + user auth:
    -- upload file(yes)
    -- download file(yes)
    -- websocket(yes)
    -- block user access(yes)
    -- block user-wise access (yes)

queue cocurrent effectiveness
    -- upload file(yes)
    -- download file(yes)
    -- websocket(yes)
"""

import os, requests, threading, time
import uvicorn

def _authorize_user(path_or_url, request, gradio_app):
    from toolbox import get_conf, default_user_name
    PATH_PRIVATE_UPLOAD, PATH_LOGGING = get_conf('PATH_PRIVATE_UPLOAD', 'PATH_LOGGING')
    sensitive_path = None
    path_or_url = os.path.relpath(path_or_url)
    if path_or_url.startswith(PATH_LOGGING):
        sensitive_path = PATH_LOGGING
    if path_or_url.startswith(PATH_PRIVATE_UPLOAD):
        sensitive_path = PATH_PRIVATE_UPLOAD
    if sensitive_path:
        token = request.cookies.get("access-token") or request.cookies.get("access-token-unsecure")
        user = gradio_app.tokens.get(token)  # get user
        allowed_users = [user, 'autogen', default_user_name]  # three user path that can be accessed
        for user_allowed in allowed_users:
            # exact match
            if f"{os.sep}".join(path_or_url.split(os.sep)[:2]) == os.path.join(sensitive_path, user_allowed):
                return True
        return False # "越权访问!"
    return True


class Server(uvicorn.Server):
    # A server that runs in a separate thread
    def install_signal_handlers(self):
        pass

    def run_in_thread(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        while not self.started:
            time.sleep(1e-3)

    def close(self):
        self.should_exit = True
        self.thread.join()


def start_app(app_block, CONCURRENT_COUNT, AUTHENTICATION, PORT, SSL_KEYFILE, SSL_CERTFILE):
    import uvicorn
    import fastapi
    import gradio as gr
    from fastapi import FastAPI
    from gradio.routes import App
    from toolbox import get_conf
    CUSTOM_PATH, PATH_LOGGING = get_conf('CUSTOM_PATH', 'PATH_LOGGING')

    # --- --- configurate gradio app block --- ---
    app_block:gr.Blocks
    app_block.ssl_verify = False
    app_block.auth_message = '请登录'
    app_block.favicon_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "docs/logo.png")
    app_block.auth = AUTHENTICATION if len(AUTHENTICATION) != 0 else None
    app_block.blocked_paths = ["config.py", "__pycache__", "config_private.py", "docker-compose.yml", "Dockerfile", f"{PATH_LOGGING}/admin"]
    app_block.dev_mode = False
    app_block.config = app_block.get_config_file()
    app_block.enable_queue = True
    app_block.queue(concurrency_count=CONCURRENT_COUNT)
    app_block.validate_queue_settings()
    app_block.show_api = False
    app_block.config = app_block.get_config_file()
    max_threads = 40
    app_block.max_threads = max(
        app_block._queue.max_thread_count if app_block.enable_queue else 0, max_threads
    )
    app_block.is_colab = False
    app_block.is_kaggle = False
    app_block.is_sagemaker = False

    gradio_app = App.create_app(app_block)

    # --- --- replace gradio endpoint to forbid access to sensitive files --- ---
    if len(AUTHENTICATION) > 0:
        dependencies = []
        endpoint = None
        for route in list(gradio_app.router.routes):
            if route.path == "/file/{path:path}":
                gradio_app.router.routes.remove(route)
            if route.path == "/file={path_or_url:path}":
                dependencies = route.dependencies
                endpoint = route.endpoint
                gradio_app.router.routes.remove(route)
        @gradio_app.get("/file/{path:path}", dependencies=dependencies)
        @gradio_app.head("/file={path_or_url:path}", dependencies=dependencies)
        @gradio_app.get("/file={path_or_url:path}", dependencies=dependencies)
        async def file(path_or_url: str, request: fastapi.Request):
            if len(AUTHENTICATION) > 0:
                if not _authorize_user(path_or_url, request, gradio_app):
                    return "越权访问!"
            return await endpoint(path_or_url, request)

    TTS_TYPE = get_conf("TTS_TYPE")
    if TTS_TYPE != "DISABLE":
        # audio generation functionality
        import httpx
        from fastapi import FastAPI, Request, HTTPException
        from starlette.responses import Response
        async def forward_request(request: Request, method: str) -> Response:
            async with httpx.AsyncClient() as client:
                try:
                    # Forward the request to the target service
                    if TTS_TYPE == "EDGE_TTS":
                        import tempfile
                        import edge_tts
                        import wave
                        import uuid
                        from pydub import AudioSegment
                        json = await request.json()
                        voice = get_conf("EDGE_TTS_VOICE")
                        tts = edge_tts.Communicate(text=json['text'], voice=voice)
                        temp_folder = tempfile.gettempdir()
                        temp_file_name = str(uuid.uuid4().hex)
                        temp_file = os.path.join(temp_folder, f'{temp_file_name}.mp3')
                        await tts.save(temp_file)
                        mp3_audio = AudioSegment.from_file(temp_file, format="mp3")
                        mp3_audio.export(temp_file, format="wav")
                        with open(temp_file, 'rb') as wav_file: t = wav_file.read()
                        os.remove(temp_file)
                        return Response(content=t)
                    if TTS_TYPE == "LOCAL_SOVITS_API":
                        # Forward the request to the target service
                        TARGET_URL = get_conf("GPT_SOVITS_URL")
                        body = await request.body()
                        resp = await client.post(TARGET_URL, content=body, timeout=60)
                        # Return the response from the target service
                        return Response(content=resp.content, status_code=resp.status_code, headers=dict(resp.headers))
                except httpx.RequestError as e:
                    raise HTTPException(status_code=400, detail=f"Request to the target service failed: {str(e)}")
        @gradio_app.post("/vits")
        async def forward_post_request(request: Request):
            return await forward_request(request, "POST")

    # --- --- app_lifespan --- ---
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def app_lifespan(app):
        async def startup_gradio_app():
            if gradio_app.get_blocks().enable_queue:
                gradio_app.get_blocks().startup_events()
        async def shutdown_gradio_app():
            pass
        await startup_gradio_app() # startup logic here
        yield  # The application will serve requests after this point
        await shutdown_gradio_app() # cleanup/shutdown logic here

    # --- --- FastAPI --- ---
    fastapi_app = FastAPI(lifespan=app_lifespan)
    fastapi_app.mount(CUSTOM_PATH, gradio_app)

    # --- --- favicon --- ---
    if CUSTOM_PATH != '/':
        from fastapi.responses import FileResponse
        @fastapi_app.get("/favicon.ico")
        async def favicon():
            return FileResponse(app_block.favicon_path)

    # --- --- uvicorn.Config --- ---
    ssl_keyfile = None if SSL_KEYFILE == "" else SSL_KEYFILE
    ssl_certfile = None if SSL_CERTFILE == "" else SSL_CERTFILE
    server_name = "0.0.0.0"
    config = uvicorn.Config(
        fastapi_app,
        host=server_name,
        port=PORT,
        reload=False,
        log_level="warning",
        ssl_keyfile=ssl_keyfile,
        ssl_certfile=ssl_certfile,
    )
    server = Server(config)
    url_host_name = "localhost" if server_name == "0.0.0.0" else server_name
    if ssl_keyfile is not None:
        if ssl_certfile is None:
            raise ValueError(
                "ssl_certfile must be provided if ssl_keyfile is provided."
            )
        path_to_local_server = f"https://{url_host_name}:{PORT}/"
    else:
        path_to_local_server = f"http://{url_host_name}:{PORT}/"
    if CUSTOM_PATH != '/':
        path_to_local_server += CUSTOM_PATH.lstrip('/').rstrip('/') + '/'
    # --- --- begin  --- ---
    server.run_in_thread()

    # --- --- after server launch --- ---
    app_block.server = server
    app_block.server_name = server_name
    app_block.local_url = path_to_local_server
    app_block.protocol = (
        "https"
        if app_block.local_url.startswith("https") or app_block.is_colab
        else "http"
    )

    if app_block.enable_queue:
        app_block._queue.set_url(path_to_local_server)

    forbid_proxies = {
        "http": "",
        "https": "",
    }
    requests.get(f"{app_block.local_url}startup-events", verify=app_block.ssl_verify, proxies=forbid_proxies)
    app_block.is_running = True
    app_block.block_thread()
