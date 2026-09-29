import json
import random
import re
import sqlite3
from contextlib import closing
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http import HTTPStatus
from pathlib import Path
from tempfile import gettempdir
from urllib.parse import urlsplit


PROJECT_DIR = Path(__file__).resolve().parent
INTENTS_PATH = PROJECT_DIR / "intents.json"
NAMES_SEED_PATH = PROJECT_DIR / "kids_names_seed.json"
DATABASE_PATH = PROJECT_DIR / "kids_names.db"
MAX_REQUEST_BYTES = 16_384
MAX_MESSAGE_LENGTH = 2_000
MATCH_THRESHOLD = 0.68
SINGLE_WORD_STOP_WORDS = {
    "a", "are", "do", "how", "i", "is", "me", "my", "the", "there",
    "what", "who", "you",
}
FALLBACK_RESPONSE = (
    "I can suggest baby names, filter by style or starting letter, or look up "
    "a name's meaning. For example, try “suggest nature names” or “names "
    "starting with A.”"
)


def load_intents():
    with INTENTS_PATH.open(encoding="utf-8") as intents_file:
        data = json.load(intents_file)

    if not isinstance(data, dict):
        raise ValueError(f"{INTENTS_PATH} must contain a JSON object.")

    intents = data.get("intents")
    if not isinstance(intents, list) or not intents:
        raise ValueError(f"{INTENTS_PATH} must contain a non-empty intents list.")

    for intent in intents:
        if (
            not isinstance(intent, dict)
            or not isinstance(intent.get("tag"), str)
            or not isinstance(intent.get("patterns"), list)
            or not isinstance(intent.get("responses"), list)
            or not intent["responses"]
            or not all(isinstance(item, str) for item in intent["patterns"])
            or not all(isinstance(item, str) for item in intent["responses"])
        ):
            raise ValueError(
                f"Each intent in {INTENTS_PATH} needs a tag, patterns, "
                "and at least one response."
            )

    return intents


def load_names_seed():
    with NAMES_SEED_PATH.open(encoding="utf-8") as seed_file:
        entries = json.load(seed_file)

    if not isinstance(entries, list):
        raise ValueError(f"{NAMES_SEED_PATH} must contain a JSON list.")

    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not all(
                isinstance(entry.get(field), str)
                for field in ("name", "gender", "meaning", "origin", "style")
            )
            or entry["gender"] not in {"girl", "boy", "neutral"}
            or not re.fullmatch(r"[A-Za-z][A-Za-z'-]*", entry["name"])
        ):
            raise ValueError(
                f"Each entry in {NAMES_SEED_PATH} needs a valid name, gender "
                "(girl, boy, or neutral), meaning, origin, and style."
            )

    return entries


def initialize_database(database_path=DATABASE_PATH):
    entries = load_names_seed()
    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)

    with closing(sqlite3.connect(database_path)) as connection:
        with connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS kids_names (
                    name TEXT PRIMARY KEY COLLATE NOCASE,
                    gender TEXT NOT NULL,
                    meaning TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    style TEXT NOT NULL
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO kids_names (name, gender, meaning, origin, style)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    gender = excluded.gender,
                    meaning = excluded.meaning,
                    origin = excluded.origin,
                    style = excluded.style
                """,
                [
                    (
                        entry["name"],
                        entry["gender"],
                        entry["meaning"],
                        entry["origin"],
                        entry["style"],
                    )
                    for entry in entries
                ],
            )


def find_names(database_path=DATABASE_PATH, gender=None, style=None, starts_with=None, limit=5):
    filters = []
    parameters = []
    if gender:
        filters.append("gender = ?")
        parameters.append(gender)
    if style:
        filters.append("style = ?")
        parameters.append(style)
    if starts_with:
        filters.append("name LIKE ?")
        parameters.append(f"{starts_with}%")
    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""

    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"""
            SELECT name, gender, meaning, origin, style
            FROM kids_names
            {where_clause}
            ORDER BY name COLLATE NOCASE
            LIMIT ?
            """,
            (*parameters, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def lookup_name(name, database_path=DATABASE_PATH):
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT name, gender, meaning, origin, style
            FROM kids_names
            WHERE name = ? COLLATE NOCASE
            """,
            (name,),
        ).fetchone()

    if row is None:
        return None
    return dict(row)


def normalize(text):
    return " ".join(re.findall(r"\w+", text.casefold()))


def extract_name_lookup(message):
    normalized_message = message.strip().casefold()
    name = r"([a-z]+(?:['-][a-z]+)*)"
    patterns = (
        rf"(?:what\s+does|what's)\s+{name}\s+mean[?.!]*",
        rf"(?:meaning|origin)\s+of\s+{name}[?.!]*",
        rf"(?:look\s+up|tell\s+me\s+about)\s+{name}[?.!]*",
    )

    for pattern in patterns:
        match = re.fullmatch(pattern, normalized_message)
        if match:
            return match.group(1), True

    match = re.fullmatch(rf"{name}[?.!]*", normalized_message)
    if match:
        return match.group(1), False
    return None, False


def extract_name_filters(message):
    normalized_message = message.casefold()
    is_name_request = bool(
        re.search(r"\b(names?|name ideas?|suggest|suggestions|recommend)\b", normalized_message)
    )
    if not is_name_request:
        return None

    gender_match = re.search(r"\b(girl|boy|neutral|unisex)\b", normalized_message)
    gender = gender_match.group(1) if gender_match else None
    if gender == "unisex":
        gender = "neutral"

    style_aliases = {
        "nature": "nature",
        "classic": "classic",
        "traditional": "classic",
        "modern": "modern",
        "short": "short",
        "strong": "strong",
        "gentle": "gentle",
    }
    style = next(
        (value for alias, value in style_aliases.items()
         if re.search(rf"\b{alias}\b", normalized_message)),
        None,
    )
    letter_match = re.search(r"\b(?:starting with|start with|beginning with)\s+([a-z])\b", normalized_message)
    starts_with = letter_match.group(1).upper() if letter_match else None
    return {"gender": gender, "style": style, "starts_with": starts_with}


def find_intent(message, intents):
    normalized_message = normalize(message)
    message_words = set(normalized_message.split())
    best_intent = None
    best_score = 0.0

    for intent in intents:
        for pattern in intent["patterns"]:
            normalized_pattern = normalize(pattern)
            pattern_words = set(normalized_pattern.split())
            if not normalized_pattern or not pattern_words:
                continue

            sequence_score = SequenceMatcher(
                None, normalized_message, normalized_pattern
            ).ratio()
            overlap = message_words & pattern_words
            pattern_coverage = len(overlap) / len(pattern_words)
            single_word_match = (
                len(message_words) == 1
                and normalized_message in pattern_words
                and normalized_message not in SINGLE_WORD_STOP_WORDS
            )
            score = max(
                sequence_score,
                pattern_coverage if len(overlap) >= 2 else 0,
                MATCH_THRESHOLD if single_word_match else 0,
            )

            if score > best_score:
                best_intent = intent
                best_score = score

    if best_score < MATCH_THRESHOLD:
        return None
    return best_intent


def create_reply(message, intents, database_path=DATABASE_PATH):
    filters = extract_name_filters(message)
    if filters is not None:
        suggestions = find_names(database_path, **filters)
        if suggestions:
            return {
                "reply": "Here are a few name ideas to get you started:",
                "tag": "name_suggestions",
                "suggestions": suggestions,
            }
        return {
            "reply": (
                "I couldn't find names matching all those filters in my starter "
                "list. Try removing a filter or choosing another starting letter."
            ),
            "tag": "no_name_suggestions",
        }

    requested_name, explicit_lookup = extract_name_lookup(message)
    if requested_name:
        name_entry = lookup_name(requested_name, database_path)
        if name_entry:
            return {
                "reply": f'{name_entry["name"]}: {name_entry["meaning"]}',
                "tag": "name_meaning",
                "name": name_entry,
            }
        if explicit_lookup:
            return {
                "reply": (
                    f'I don\'t have "{requested_name.title()}" in my starter list '
                    "yet. Try asking me for name ideas instead."
                ),
                "tag": "name_not_found",
            }

    intent = find_intent(message, intents)
    if intent:
        return {"reply": random.choice(intent["responses"]), "tag": intent["tag"]}
    if requested_name:
        return {
            "reply": (
                f'I don\'t have "{requested_name.title()}" in my starter list '
                "yet. Try asking me for name ideas instead."
            ),
            "tag": "name_not_found",
        }
    return {"reply": FALLBACK_RESPONSE, "tag": "fallback"}


def chat_response(content_type, body, database_path):
    media_type = content_type.split(";", 1)[0].strip().casefold()
    if media_type != "application/json":
        return HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {
            "error": "Send a message as application/json."
        }

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return HTTPStatus.BAD_REQUEST, {"error": "The request body must be valid JSON."}

    if not isinstance(payload, dict) or not isinstance(payload.get("message"), str):
        return HTTPStatus.BAD_REQUEST, {"error": "Include a message as text."}

    message = payload["message"].strip()
    if not message:
        return HTTPStatus.BAD_REQUEST, {"error": "Type a message before sending."}
    if len(message) > MAX_MESSAGE_LENGTH:
        return HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {
            "error": f"Messages must be {MAX_MESSAGE_LENGTH} characters or fewer."
        }

    try:
        return HTTPStatus.OK, create_reply(message, load_intents(), database_path)
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"Unable to load chatbot data: {error}")
        return HTTPStatus.INTERNAL_SERVER_ERROR, {
            "error": "The chatbot could not load its data."
        }


def wsgi_app(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = urlsplit(environ.get("PATH_INFO", "/")).path
    if method == "GET" and path == "/":
        try:
            body = (PROJECT_DIR / "index.html").read_bytes()
        except OSError as error:
            print(f"Unable to load the chatbot page: {error}")
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            body = b"The chatbot page could not be read."
        else:
            status = HTTPStatus.OK
        content_type = "text/html; charset=utf-8"
    elif method == "POST" and path == "/api/chat":
        try:
            content_length = int(environ.get("CONTENT_LENGTH") or "0")
        except ValueError:
            content_length = -1

        if content_length < 0:
            status = HTTPStatus.BAD_REQUEST
            payload = {"error": "A valid Content-Length is required."}
        elif content_length > MAX_REQUEST_BYTES:
            status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE
            payload = {"error": "The request body is too large."}
        else:
            database_path = Path(gettempdir()) / "kids-name-prediction.db"
            try:
                initialize_database(database_path)
                body = environ["wsgi.input"].read(content_length)
                status, payload = chat_response(
                    environ.get("CONTENT_TYPE", ""), body, database_path
                )
            except (OSError, ValueError, sqlite3.Error) as error:
                print(f"Unable to initialize the names database: {error}")
                status = HTTPStatus.INTERNAL_SERVER_ERROR
                payload = {"error": "The name suggestions could not be loaded."}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        content_type = "application/json; charset=utf-8"
    else:
        status = HTTPStatus.NOT_FOUND
        body = b"Not found."
        content_type = "text/plain; charset=utf-8"

    start_response(
        f"{status.value} {status.phrase}",
        [
            ("Content-Type", content_type),
            ("Content-Length", str(len(body))),
            ("X-Content-Type-Options", "nosniff"),
        ],
    )
    return [body]


app = wsgi_app


class ChatbotHandler(BaseHTTPRequestHandler):
    database_path = DATABASE_PATH

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if urlsplit(self.path).path != "/":
            self.send_error(404)
            return

        page_path = PROJECT_DIR / "index.html"
        try:
            page = page_path.read_bytes()
        except OSError:
            self.send_error(500, "The chatbot page could not be read.")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(page)

    def do_POST(self):
        if urlsplit(self.path).path != "/api/chat":
            self.send_json(404, {"error": "Not found."})
            return

        content_type = self.headers.get("Content-Type", "").split(";", 1)[0]

        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.send_json(400, {"error": "A valid Content-Length is required."})
            return

        if content_length < 0:
            self.send_json(400, {"error": "The request body is invalid."})
            return
        if content_length > MAX_REQUEST_BYTES:
            self.send_json(413, {"error": "The request body is too large."})
            return

        if content_length < 0:
            self.send_json(400, {"error": "The request body is invalid."})
            return
        if content_length > MAX_REQUEST_BYTES:
            self.send_json(413, {"error": "The request body is too large."})
            return

        status, payload = chat_response(
            content_type, self.rfile.read(content_length), self.database_path
        )
        self.send_json(status.value, payload)

    def log_message(self, format_string, *args):
        print(f"{self.address_string()} - {format_string % args}")


def main():
    initialize_database()
    server = ThreadingHTTPServer(("127.0.0.1", 8000), ChatbotHandler)
    print("Kids' Name Prediction is running at http://127.0.0.1:8000")
    print(f"Local names database: {DATABASE_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping chatbot.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
