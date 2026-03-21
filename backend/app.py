# backend/app.py
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import asyncio
import aiohttp
import os

FAL_KEY = os.getenv("FAL_KEY")  # ключ для генерации изображений

app = FastAPI()
queue = asyncio.Queue()

class Prompt(BaseModel):
    prompt: str
    mode: str

# ===== Очистка запроса =====
def clean_prompt(prompt: str):
    replacements = {
        "gun": "device",
        "weapon": "device",
        "kill": "defeat",
    }
    prompt = prompt.lower()
    for k, v in replacements.items():
        prompt = prompt.replace(k, v)
    return prompt

# ================= IMAGE =================
async def generate_image(prompt: str):
    prompt = clean_prompt(prompt)
    url = "https://queue.fal.run/fal-ai/nano-banana"
    headers = {"Authorization": f"Key {FAL_KEY}", "Content-Type": "application/json"}
    payload = {"prompt": prompt, "num_images": 1}

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as r:
            data = await r.json()
            request_id = data.get("request_id")

        status_url = f"{url}/requests/{request_id}/status"
        result_url = f"{url}/requests/{request_id}"

        for _ in range(120):
            async with session.get(status_url, headers=headers) as s:
                status = await s.json()
                if status.get("status") == "COMPLETED":
                    async with session.get(result_url, headers=headers) as r2:
                        result = await r2.json()
                        return result["images"][0]["url"]
            await asyncio.sleep(1)

    return "error"

# ================= VIDEO =================
async def generate_video(prompt: str):
    prompt = clean_prompt(prompt)
    return f"VIDEO GENERATED: {prompt}"

# ================= MUSIC =================
async def generate_music(prompt: str):
    prompt = clean_prompt(prompt)
    return f"MUSIC GENERATED: {prompt}"

# ================== ROUTES ==================
@app.get("/", response_class=HTMLResponse)
async def home():
    return open("index.html", "r", encoding="utf-8").read()

@app.post("/generate")
async def generate(data: Prompt):
    if data.mode == "image":
        url = await generate_image(data.prompt)
        return {"type": "image", "url": url}
    elif data.mode == "video":
        result = await generate_video(data.prompt)
        return {"type": "text", "result": result}
    elif data.mode == "music":
        result = await generate_music(data.prompt)
        return {"type": "text", "result": result}
    return {"result": "error"}

# ================== FRONTEND ==================
html_code = """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sosai AI</title>
<style>
body {margin:0;font-family:Arial;background:#0f0f0f;color:white;}
.header{padding:20px;text-align:center;font-size:24px;background:#1a1a1a;}
.container{padding:20px;}
button{width:100%;padding:15px;margin:5px 0;border:none;border-radius:10px;background:#00ffcc;font-size:16px;}
input{width:100%;padding:15px;border-radius:10px;border:none;margin-bottom:10px;}
.card{background:#1a1a1a;padding:15px;border-radius:15px;margin-top:10px;}
img{max-width:90%;margin-top:10px;border-radius:10px;}
</style>
</head>
<body>
<div class="header">🚀 Sosai AI</div>
<div class="container">
<div class="card">
<h3>🎨 Генерация</h3>
<button onclick="setMode('image')">Изображение</button>
<button onclick="setMode('video')">Видео</button>
<button onclick="setMode('music')">Музыка</button>
</div>
<div class="card">
<h3>✏ Ввод</h3>
<input id="prompt" placeholder="Введите запрос...">
<button onclick="generate()">Создать</button>
</div>
<div class="card">
<h3>👤 Аккаунт</h3>
<p>Free: 5 генераций</p>
<p>Premium: 200 генераций</p>
<button onclick="buy()">Купить Premium</button>
</div>
<div class="card">
<h3>📊 Статус</h3>
<p id="status">Ожидание...</p>
<div id="result"></div>
</div>
</div>
<script>
let mode='image';
function setMode(m){mode=m;document.getElementById('status').innerText='Выбран режим: '+m;}
async function generate(){
    let prompt=document.getElementById('prompt').value;
    document.getElementById('status').innerText='⏳ Генерация...';
    document.getElementById('result').innerHTML='';
    let res=await fetch('/generate',{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({prompt,prompt:prompt,mode})
    });
    let data=await res.json();
    if(data.type==='image'){document.getElementById('result').innerHTML=`<img src="${data.url}"/>`;}
    else if(data.type==='text'){document.getElementById('result').innerText=data.result;}
    else{document.getElementById('result').innerText='❌ Ошибка генерации';}
    document.getElementById('status').innerText='✅ Готово';
}
function buy(){alert('💎 Premium: 500 руб / 30 дней');}
</script>
</body>
</html>
"""
with open("index.html", "w", encoding="utf-8") as f:
    f.write(html_code)
