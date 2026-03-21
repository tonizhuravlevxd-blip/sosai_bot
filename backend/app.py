# ===================== BACKEND (FastAPI) =====================
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import asyncio

app = FastAPI()

# ===== ТВОЯ ОЧЕРЕДЬ (можно связать с ботом) =====
queue = asyncio.Queue()

class Prompt(BaseModel):
    prompt: str
    mode: str

@app.get("/", response_class=HTMLResponse)
async def home():
    return open("index.html", "r", encoding="utf-8").read()

@app.post("/generate")
async def generate(data: Prompt):
    # тут можно вставить твой fal_generate
    await asyncio.sleep(2)

    return {"result": f"✅ Готово: {data.prompt} ({data.mode})"}


# ===================== FRONTEND (index.html) =====================

html_code = """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sosai AI</title>

<style>
body {
    margin: 0;
    font-family: Arial;
    background: #0f0f0f;
    color: white;
}

.header {
    padding: 20px;
    text-align: center;
    font-size: 24px;
    background: #1a1a1a;
}

.container {
    padding: 20px;
}

button {
    width: 100%;
    padding: 15px;
    margin: 5px 0;
    border: none;
    border-radius: 10px;
    background: #00ffcc;
    font-size: 16px;
}

input {
    width: 100%;
    padding: 15px;
    border-radius: 10px;
    border: none;
    margin-bottom: 10px;
}

.card {
    background: #1a1a1a;
    padding: 15px;
    border-radius: 15px;
    margin-top: 10px;
}
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
</div>

</div>

<script>
let mode = 'image';

function setMode(m) {
    mode = m;
    document.getElementById('status').innerText = 'Выбран режим: ' + m;
}

async function generate() {
    let prompt = document.getElementById('prompt').value;

    document.getElementById('status').innerText = '⏳ Генерация...';

    let res = await fetch('/generate', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({prompt: prompt, mode: mode})
    });

    let data = await res.json();

    document.getElementById('status').innerText = data.result;
}

function buy() {
    alert('💎 Premium: 500 руб / 30 дней');
}
</script>

</body>
</html>
"""

with open("index.html", "w", encoding="utf-8") as f:
    f.write(html_code)
