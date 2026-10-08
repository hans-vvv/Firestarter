from __future__ import annotations

from app.web import create_app

app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
