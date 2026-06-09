FROM python:3.12-slim

WORKDIR /app

# Bağımlılıkları önce kur (katman cache için)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn==23.0.0

# Uygulama kodu (çalışma anında volume ile üzerine bindirilir; build içinde de bulunsun)
COPY . .

EXPOSE 5050

# Flask debug yerine üretim için gunicorn. app.py içindeki Flask nesnesi: app
CMD ["gunicorn", "--bind", "0.0.0.0:5050", "--workers", "2", "--timeout", "120", "app:app"]
