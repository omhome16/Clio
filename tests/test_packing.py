# tests/test_packing.py
from clio.config import Limits
from clio.graph import build_repo_graph
from clio.packing import pack_entrypoints, pack_risks


def _repo(write_tree):
    return write_tree(
        {
            "app/__init__.py": "",
            "app/service.py": (
                "def greet(name):\n"
                "    return f'hello {name}'\n"
                "\n"
                "def broken():\n"
                "    try:\n"
                "        return 1 / 0\n"
                "    except:\n"
                "        pass\n"
            ),
            "app/main.py": (
                "from app.service import greet\n"
                "\n"
                "def run():\n"
                "    return greet('clio')\n"
                "\n"
                "TODO: wire up the real parser\n"
                "APP_PASSWORD = 'hunter2'\n"
            ),
            "README.md": "# Demo\nA tiny demo service.\n",
        }
    )


def test_pack_risks_detects_patterns(write_tree):
    root = _repo(write_tree)
    graph = build_repo_graph(root)
    limits = Limits(aspect_pack_chars=4000)
    text = pack_risks(root, graph, limits)
    assert "bare except" in text
    assert "hardcoded credential" in text
    assert "todo" in text


def test_pack_risks_fits_budget(write_tree):
    root = _repo(write_tree)
    graph = build_repo_graph(root)
    text = pack_risks(root, graph, Limits(aspect_pack_chars=800))
    assert len(text) <= 800 + 64


def test_pack_entrypoints_prefers_main_and_readme(write_tree):
    root = _repo(write_tree)
    graph = build_repo_graph(root)
    text = pack_entrypoints(root, graph, Limits(aspect_pack_chars=8000))
    assert "app/main.py" in text
    assert "[doc]" in text


def test_packs_empty_workspace(tmp_path):
    graph = build_repo_graph(tmp_path)
    limits = Limits()
    assert pack_risks(tmp_path, graph, limits).strip()
    assert pack_entrypoints(tmp_path, graph, limits).strip()
