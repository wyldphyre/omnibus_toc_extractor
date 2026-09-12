import os
import zipfile

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import RequestEntityTooLarge

from app import __version__
from app.epub_parser import EpubParser, EpubTooLargeError

app = Flask(__name__, static_folder=None)

# 150 MB upload limit
MAX_UPLOAD_MB = 150
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)


@app.route("/api/version")
def version():
    return jsonify({"version": __version__})


@app.errorhandler(RequestEntityTooLarge)
def upload_too_large(_exc):
    # Flask's default response here is HTML; the front end expects JSON so it
    # can show the real reason instead of a generic failure.
    return jsonify({"error": f"File is too large ({MAX_UPLOAD_MB} MB maximum)."}), 413


@app.route("/api/extract", methods=["POST"])
def extract():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    f = request.files["file"]

    if not f.filename or not f.filename.lower().endswith(".epub"):
        return jsonify({"error": "File must be an .epub"}), 400

    try:
        with EpubParser(f.read()) as parser:
            data = parser.parse()
    except EpubTooLargeError:
        return jsonify({"error": "This epub's contents are too large to process."}), 422
    except zipfile.BadZipFile:
        return jsonify({"error": "That file isn't a valid epub (not a ZIP archive)."}), 422
    except Exception:
        # The underlying message can carry archive paths and parser internals,
        # so it goes to the log rather than to the client.
        app.logger.exception("Failed to parse epub %r", f.filename)
        return jsonify({"error": "Could not read the table of contents from this epub."}), 422

    return jsonify(data)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
