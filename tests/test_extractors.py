# tests/test_extractors.py
from clio.extractors import (
    detect_language,
    extract,
    foreign_module_name,
)
from clio.graph import build_repo_graph


def test_detect_language_by_suffix(tmp_path):
    assert detect_language(tmp_path / "a.py") == "python"
    assert detect_language(tmp_path / "a.js") == "javascript"
    assert detect_language(tmp_path / "a.tsx") == "typescript"
    assert detect_language(tmp_path / "a.go") == "go"
    assert detect_language(tmp_path / "a.rs") == "rust"
    assert detect_language(tmp_path / "a.rb") == "ruby"
    assert detect_language(tmp_path / "a.cpp") == "cpp"
    assert detect_language(tmp_path / "README.md") is None


def test_foreign_module_name(tmp_path):
    root = tmp_path / "repo"
    (root / "src" / "lib").mkdir(parents=True)
    assert foreign_module_name(root / "src" / "lib" / "util.ts", root) == "src/lib/util"


def test_js_symbols_and_relative_import_resolution():
    text = (
        "import { helper } from './helper';\n"
        "import React from 'react';\n"
        "export function format(x) {\n"
        "  return helper(x);\n"
        "}\n"
        "export const greet = (name) => `hi ${name}`;\n"
    )
    symbols, imports = extract(text, "javascript", importer_dir="src/util")
    assert ("format", "function", 3) in symbols
    assert ("greet", "function", 6) in symbols
    assert imports == ["src/util/helper", "react"]


def test_ts_relative_import_traversal():
    text = "import x from '../../shared/types';\n"
    _, imports = extract(text, "typescript", importer_dir="a/b/c")
    assert imports == ["a/shared/types"]


def test_go_imports_and_symbols():
    text = (
        'package main\n\n'
        'import "fmt"\n'
        'import (\n\t"os"\n\t"strings"\n)\n\n'
        "func main() {\n\tfmt.Println(hello())\n}\n\n"
        "func hello() string { return \"hi\" }\n"
    )
    symbols, imports = extract(text, "go")
    assert imports == ["fmt", "os", "strings"]
    assert ("main", "function", 9) in symbols
    assert ("hello", "function", 13) in symbols


def test_go_internal_import_prefix_stripped():
    text = (
        'package util\n\n'
        'import (\n'
        '\t"github.com/acme/app/cmd/util"\n'
        '\t"github.com/acme/app/internal/keys"\n'
        '\t"fmt"\n'
        ')\n'
    )
    _, imports = extract(text, "go", go_module="github.com/acme/app")
    assert imports == ["cmd/util", "internal/keys", "fmt"]


def test_go_module_prefix_only_when_set():
    text = 'package util\nimport "github.com/acme/app/cmd/util"\n'
    _, imports = extract(text, "go")
    assert imports == ["github.com/acme/app/cmd/util"]


def test_go_module_prefix_via_repo_graph(tmp_path, write_tree):
    root = write_tree({
        "go.mod": "module github.com/acme/app\n",
        "cmd/util/util.go": 'package util\n\nimport "github.com/acme/app/internal/keys"\n\nfunc helper() string { return keys.K() }\n',
        "internal/keys/keys.go": "package keys\n\nfunc K() string { return \"k\" }\n",
    })
    graph = build_repo_graph(root)
    assert graph.imports["cmd/util/util"] == ["internal/keys"]
    assert graph.imports["internal/keys/keys"] == []


def test_java_imports_and_class():
    text = (
        "package com.example;\n"
        "import java.util.List;\n"
        "public class Main {\n"
        "    public static void main(String[] args) {}\n"
        "}\n"
    )
    symbols, imports = extract(text, "java")
    assert imports == ["java.util.List"]
    assert ("Main", "class", 3) in symbols
    assert ("main", "method", 4) in symbols


def test_rust_symbols():
    text = (
        "pub fn main() {\n    helper();\n}\n\n"
        "pub struct Config {\n    pub name: String,\n}\n\n"
        "fn helper() {}\n"
    )
    symbols, _ = extract(text, "rust")
    assert ("main", "function", 1) in symbols
    assert ("Config", "class", 5) in symbols
    assert ("helper", "function", 9) in symbols


def test_mixed_language_repo_graph(tmp_path, write_tree):
    root = write_tree({
        "app/main.py": "from app.service import greet\n\ndef run():\n    return greet('x')\n",
        "app/service.py": "def greet(name):\n    return f'hi {name}'\n",
        "src/util.ts": "import { helper } from './helper';\nexport function format(x) { return helper(x); }\n",
        "src/helper.ts": "export function helper(x) { return x * 2; }\n",
        "cmd/util.go": 'package main\nimport "fmt"\nfunc main() {\n\tfmt.Println(hello())\n}\n',
        "lib/math.rb": "class MathX\n  def double(x)\n    x * 2\n  end\nend\n",
        "README.md": "# not code\n",
    })
    graph = build_repo_graph(root)
    assert set(graph.modules) == {
        "app.main", "app.service", "src/util", "src/helper", "cmd/util", "lib/math",
    }
    assert graph.languages["app.main"] == "python"
    assert graph.languages["src/util"] == "typescript"
    assert graph.languages["cmd/util"] == "go"
    assert graph.language_stats() == {"go": 1, "python": 2, "ruby": 1, "typescript": 2}
    assert graph.imports["src/util"] == ["src/helper"]
    assert graph.imports["app.main"] == ["app.service.greet"]
    names = {(s.module, s.name) for s in graph.symbols}
    assert ("src/util", "format") in names
    assert ("cmd/util", "main") in names
    assert ("lib/math", "MathX") in names
    assert graph.call_count == 1  # python ast tier only


def test_clusters_are_path_aware(tmp_path, write_tree):
    root = write_tree({
        "cmd/util.go": "package main\n",
        "cmd/other.go": "package other\n",
        "src/util.ts": "export const a = 1;\n",
        "web/app.py": "def f():\n    pass\n",
    })
    from clio.clustering import cluster_by_package
    graph = build_repo_graph(root)
    assert [c.name for c in cluster_by_package(graph)] == ["cmd", "src", "web"]
