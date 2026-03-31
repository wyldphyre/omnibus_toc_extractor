import os

from flask import Flask, jsonify, request, send_from_directory

from app.epub_parser import EpubParser

app = Flask(__name__, static_folder=None)

# 150 MB upload limit
app.config["MAX_CONTENT_LENGTH"] = 150 * 1024 * 1024

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)


@app.route("/api/extract", methods=["POST"])
def extract():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    f = request.files["file"]

    if not f.filename or not f.filename.lower().endswith(".epub"):
        return jsonify({"error": "File must be an .epub"}), 400

    try:
        data = EpubParser(f.read()).parse()
    except Exception as exc:
        return jsonify({"error": f"Failed to parse epub: {exc}"}), 422

    return jsonify(data)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
