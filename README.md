# Warehouse Products Web App (CRUD + PostgreSQL)

A small, ready-to-run warehouse web app:
- Add / edit / delete products
- Track stock quantity
- Track sales entries and see **last 30 days sales**
- Data is stored in **PostgreSQL**
- Clean UI (Bootstrap) + search + sorting

## 1) Install
Create and activate a virtual environment (recommended), then:

```bash
pip install -r requirements.txt
```

## 2) Configure PostgreSQL
Set `DATABASE_URL` before starting the app.

Example:
```bash
DATABASE_URL=postgresql+psycopg://uzum:YOUR_PASSWORD@127.0.0.1:5432/uzum
```

## 3) Run
```bash
python app.py
```

Open:
- http://127.0.0.1:5000

## 4) Docker
```bash
docker compose up --build -d
```

## 5) Notes
- PostgreSQL is required for all runtimes.

## Fields
Product:
- name
- sku
- barcode
- quantity
- image_url

Sales:
- product_id
- date
- qty_sold

## API (used by the UI)
- GET    /api/products
- POST   /api/products
- GET    /api/products/<id>
- PUT    /api/products/<id>
- DELETE /api/products/<id>

- POST   /api/products/<id>/sales
- GET    /api/products/<id>/sales?days=30

- GET    /api/summary?days=30   (sales per product for the last N days)


## Uzum API Access

This project fetches Uzum data directly from the Flask backend using the saved Uzum seller token.
