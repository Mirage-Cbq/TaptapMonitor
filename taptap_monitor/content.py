from dataclasses import asdict, dataclass
from html.parser import HTMLParser
import hashlib
import json


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.media = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in {"br", "p", "div", "li"}:
            self.parts.append("\n")
        if tag == "img":
            attributes = dict(attrs)
            self.parts.append(attributes.get("alt", "[图片]"))
            url = attributes.get("data-origin-url") or attributes.get("src")
            if url:
                self.media.append(url)


def text_of(contents: dict | None) -> str:
    contents = contents or {}
    if contents.get("raw_text"):
        return contents["raw_text"].strip()
    if contents.get("text"):
        parser = _Text()
        parser.feed(contents["text"])
        return "".join(parser.parts).strip()

    def rich(nodes):
        parts = []
        for node in nodes or []:
            if node.get("type") == "image":
                parts.append("[图片]")
            children = node.get("children")
            parts.append(rich(children) if children else node.get("text", ""))
            if node.get("type") == "paragraph":
                parts.append("\n")
        return "".join(parts)

    return rich(contents.get("json")).strip()


def media_of(contents: dict | None) -> list[str]:
    contents = contents or {}
    result = [x.get("original_url") or x.get("url") for x in contents.get("images", [])]
    parser = _Text()
    parser.feed(contents.get("text", ""))
    result.extend(parser.media)

    def walk(nodes):
        for node in nodes or []:
            if node.get("type") == "image":
                image = node.get("info", {}).get("img", {})
                result.append(image.get("original_url") or image.get("url"))
            walk(node.get("children"))

    walk(contents.get("json"))
    return list(dict.fromkeys(url for url in result if url))


@dataclass
class Item:
    app_id: int
    kind: str
    source_id: str
    title: str
    body: str
    url: str
    author: str = ""
    author_id: str = ""
    score: int | None = None
    parent_id: str = ""
    published_at: int = 0
    source_updated_at: int = 0
    official: bool = False
    media: list[str] | None = None

    def payload(self):
        return asdict(self)

    def fingerprint(self):
        # Counters and timestamps change independently of content edits.
        relevant = {k: getattr(self, k) for k in ("title", "body", "score", "media", "official")}
        return hashlib.sha256(json.dumps(relevant, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def review_item(app_id: int, moment: dict) -> Item:
    review = moment["review"]
    user = moment["author"]["user"]
    return Item(
        app_id, "review", str(review["id"]), "", text_of(review.get("contents")),
        f"https://www.taptap.cn/review/{review['id']}", user["name"], str(user["id"]),
        score=review.get("score"), published_at=moment["created_time"],
        source_updated_at=moment.get("edited_time", moment["created_time"]),
        official=moment.get("is_official", False),
        media=media_of(review.get("contents")),
    )


def comment_item(app_id: int, kind: str, parent_id: str, comment: dict, url: str) -> Item:
    user = comment.get("author", {})
    user = user.get("user", user)
    return Item(
        app_id, kind, str(comment.get("id_str") or comment["id"]), "", text_of(comment.get("contents")),
        url, user.get("name", ""), str(user.get("id", "")), parent_id=str(parent_id),
        published_at=comment["created_time"],
        source_updated_at=comment.get("updated_time", comment["created_time"]),
        official=comment.get("is_official", False) or user.get("is_moderator", False),
        media=media_of(comment.get("contents")),
    )
