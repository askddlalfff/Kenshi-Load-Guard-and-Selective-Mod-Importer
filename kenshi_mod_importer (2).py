#!/usr/bin/env python3
"""Kenshi 模组选择性导入工具，Python 3.10+，只需标准库。

Windows: 将本文件保存为 kenshi_mod_importer.py，运行 python kenshi_mod_importer.py。
打开来源 mod2.mod、自己的 mod1.mod，双击勾选物品、建筑、人物、发型等，
可选一个 mod1 人种接收发型，按按钮生成 Kenshi/mods/mod1_merged/mod1_merged.mod。
启动器中启用生成的模组，停用原 mod1，避免两个版本同时加载。
始终保留原 mod1 和 mod2，不修改游戏安装文件。输出附带 导入报告.json。

记录按 String ID 递归关联。源资源中被记录明确指向的文件自动复制并改路径；
模型内部材质、骨骼、外部模组依赖不能总从 .mod 推断，可勾选复制所有来源资源。
完成后请用 FCS 打开生成的模组，并在游戏内检查物品、建筑动作及外观。

格式参考：Weaver (Steam 797652627)；独立对照
Superfly-Johnson/kenshi-mod-tools 和 LucasSKrewer/kenshi-modkit，
本文件不包含这些项目的源码。支持 FCS 格式 16、17，保留原文件头附加信息。
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

KINDS = {0: "建筑", 1: "人物", 2: "武器", 3: "护甲", 4: "物品", 5: "动物动画",
         6: "发型/挂件", 7: "人种", 24: "动作动画", 29: "建筑部件", 62: "建筑功能",
         89: "头部", 105: "动画事件", 112: "基础动画"}
FIELD_FORMATS = ("?", "f", "i", "fff", "ffff", "s", "s")
FIELD_NAMES = ("布尔", "小数", "整数", "向量3", "向量4", "字符串", "文件")
REMOVED = (2147483647,) * 3
MAX_COUNT = 2_000_000
MAX_STRING = 16_000_000
ASSET_EXT = {".mesh", ".skeleton", ".material", ".dds", ".png", ".jpg",
             ".jpeg", ".xml", ".phs", ".anim", ".wav", ".ogg", ".tga"}


class FormatError(ValueError):
    pass


class Reader:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def unpack(self, fmt: str):
        fmt = "<" + fmt
        size = struct.calcsize(fmt)
        if self.pos + size > len(self.data):
            raise FormatError("文件截断")
        result = struct.unpack_from(fmt, self.data, self.pos)
        self.pos += size
        return result[0] if len(result) == 1 else result

    def number(self, limit=MAX_COUNT):
        n = self.unpack("i")
        if not 0 <= n <= limit:
            raise FormatError(f"异常的字段数量或长度：{n}")
        return n

    def string(self):
        n = self.number(MAX_STRING)
        if self.pos + n > len(self.data):
            raise FormatError("字符串超出文件范围")
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b.decode("utf-8", "surrogateescape")


def pack(fmt, value):
    if fmt == "s":
        b = value.encode("utf-8", "surrogateescape")
        return struct.pack("<i", len(b)) + b
    if fmt in ("fff", "ffff"):
        return struct.pack("<" + fmt, *value)
    return struct.pack("<" + fmt, value)


@dataclasses.dataclass
class Record:
    count: int
    kind: int
    rid: int
    name: str
    sid: str
    datatype: int
    fields: list
    extras: list
    instances: list
    raw: bytes = b""

    def to_bytes(self):
        out = bytearray()
        for val in (self.count, self.kind, self.rid):
            out += pack("i", val)
        out += pack("s", self.name) + pack("s", self.sid) + pack("i", self.datatype)
        for fmt, rows in zip(FIELD_FORMATS, self.fields):
            out += pack("i", len(rows))
            for key, val in rows:
                out += pack("s", key) + pack(fmt, val)
        out += pack("i", len(self.extras))
        for category, entries in self.extras:
            out += pack("s", category) + pack("i", len(entries))
            for key, values in entries:
                out += pack("s", key) + struct.pack("<iii", *values)
        out += pack("i", len(self.instances))
        for sid, target, pos, rot, states in self.instances:
            out += pack("s", sid) + pack("s", target) + pack("fff", pos) + pack("ffff", rot)
            out += pack("i", len(states))
            for state in states:
                out += pack("s", state)
        return bytes(out)


def parse_record(r: Reader):
    start = r.pos
    count, kind, rid = r.unpack("i"), r.unpack("i"), r.unpack("i")
    name, sid, datatype = r.string(), r.string(), r.unpack("i")
    fields = []
    for fmt in FIELD_FORMATS:
        fields.append([(r.string(), r.string() if fmt == "s" else r.unpack(fmt))
                       for _ in range(r.number())])
    extras = []
    for _ in range(r.number()):
        category = r.string()
        entries = [(r.string(), r.unpack("iii")) for _ in range(r.number())]
        extras.append((category, entries))
    instances = []
    for _ in range(r.number()):
        instance_id, target = r.string(), r.string()
        pos, rot = r.unpack("fff"), r.unpack("ffff")
        states = [r.string() for _ in range(r.number())]
        instances.append((instance_id, target, pos, rot, states))
    rec = Record(count, kind, rid, name, sid, datatype, fields, extras, instances,
                 r.data[start:r.pos])
    if rec.to_bytes() != rec.raw:
        raise FormatError(f"记录 {sid} 无法无损往返，已中止")
    return rec


@dataclasses.dataclass
class Mod:
    path: Path
    prefix: bytes
    records: list[Record]
    author: str
    description: str
    dependencies: str
    references: str

    @property
    def by_id(self):
        return {rec.sid: rec for rec in self.records}


def load_mod(path):
    path = Path(path).resolve()
    if path.suffix.lower() != ".mod":
        raise FormatError("请选择 .mod 文件；不支持直接载入存档")
    r = Reader(path.read_bytes())
    file_type = r.unpack("i")
    if file_type not in (16, 17):
        raise FormatError(f"无法解析此文件：文件头类型为 {file_type}；"
                          "目前支持 FCS .mod 类型 16 和 17。"
                          f"开头 16 字节：{r.data[:16].hex(' ')}")
    if file_type == 17:
        # The value at offset 4 is the record start offset minus 16. The
        # extra header is kept byte-for-byte, including opaque merge/delete
        # metadata. The record count is immediately before the first record.
        data_offset = r.unpack("i") + 16
        if data_offset < 12 or data_offset > len(r.data):
            raise FormatError(f"类型 17 的记录起始位置异常：{data_offset}")
    r.unpack("i")  # mod version
    author, description, dependencies, references = (r.string() for _ in range(4))
    if file_type == 17:
        if data_offset - 4 < r.pos:
            raise FormatError("类型 17 的文件头长度与元数据冲突")
        r.pos = data_offset - 4  # preserve the opaque header as part of prefix
    else:
        r.unpack("i")  # preserved FCS flags
    count_pos = r.pos
    count = r.number()
    records = [parse_record(r) for _ in range(count)]
    if r.pos != len(r.data):
        raise FormatError("文件存在无法解析的尾部数据，拒绝改写")
    ids = [rec.sid for rec in records]
    if len(ids) != len(set(ids)):
        raise FormatError("模组内存在重复 String ID，拒绝合并")
    return Mod(path, r.data[:count_pos], records, author, description, dependencies, references)


def outgoing(rec, available):
    refs = set()
    for _, entries in rec.extras:
        for key, vals in entries:
            if vals != REMOVED and key in available:
                refs.add(key)
    for _, target, _, _, _ in rec.instances:
        if target in available:
            refs.add(target)
    for rows in rec.fields[5:]:
        for _, val in rows:
            if val in available:
                refs.add(val)
    return refs


def choose_records(source, target, roots):
    src, dst = source.by_id, target.by_id
    chosen, pending = set(), list(roots)
    while pending:
        sid = pending.pop()
        if sid in chosen or sid not in src:
            continue
        if sid in dst and sid not in roots and src[sid].datatype == -2147483646:
            continue  # already provided by mod1; a changed record still supplies a patch
        chosen.add(sid)
        pending.extend(outgoing(src[sid], src))
    collisions = [sid for sid in chosen if sid in dst and src[sid].datatype == -2147483646]
    if collisions:
        raise FormatError("新建记录的 String ID 与目标冲突：" + ", ".join(collisions[:12]))
    return chosen


def merge_changed(base: Record, patch: Record):
    result = copy.deepcopy(base)
    if patch.kind != base.kind:
        raise FormatError(f"String ID 同名但记录类型不同：{patch.sid}")
    if patch.datatype == -2147483645:
        result.name = patch.name
    for idx, rows in enumerate(patch.fields):
        current = list(result.fields[idx])
        positions = {k: i for i, (k, _) in enumerate(current)}
        for key, val in rows:
            if key in positions:
                current[positions[key]] = (key, val)
            else:
                current.append((key, val))
        result.fields[idx] = current
    cats = {k: i for i, (k, _) in enumerate(result.extras)}
    for category, entries in patch.extras:
        if category not in cats:
            cats[category] = len(result.extras)
            result.extras.append((category, []))
        index = cats[category]
        merged = list(result.extras[index][1])
        indexes = {k: i for i, (k, _) in enumerate(merged)}
        for key, values in entries:
            if key in indexes:
                merged[indexes[key]] = (key, values)
            else:
                merged.append((key, values))
        result.extras[index] = (category, merged)
    if patch.instances:
        raise FormatError(f"覆盖记录 {patch.sid} 包含场景实例；请在 FCS 中手动合并")
    return result


def bind_hairs(race: Record, hairs: list[Record]):
    race = copy.deepcopy(race)
    category = "hairs"
    index = next((i for i, (name, _) in enumerate(race.extras) if name == category), None)
    if index is None:
        race.extras.append((category, []))
        index = len(race.extras) - 1
    entries = list(race.extras[index][1])
    for hair in hairs:
        if hair.kind == 6 and hair.sid not in {name for name, _ in entries}:
            entries.append((hair.sid, (100, 100, 0)))
    race.extras[index] = (category, entries)
    return race


def asset_index(folder):
    result = {}
    for path in folder.rglob("*"):
        if path.is_file() and path.suffix.lower() != ".mod":
            result[path.relative_to(folder).as_posix().casefold()] = path
    return result


def file_ref(value, folder, index, mod_stem):
    """Return (asset, relative path) only for an unambiguous file owned by folder."""
    normalized = value.strip().strip('"').replace("\\", "/").lstrip("./")
    if not normalized or Path(normalized).suffix.lower() not in ASSET_EXT:
        return None
    parts = normalized.split("/")
    mod_ids = {folder.name.casefold(), mod_stem.casefold()}
    if len(parts) >= 3 and parts[0].casefold() == "mods":
        if parts[1].casefold() not in mod_ids:
            return None
        normalized = "/".join(parts[2:])
    if ":" in normalized or normalized.startswith("/") or ".." in normalized.split("/"):
        return None
    path = index.get(normalized.casefold())
    if path:
        return path, path.relative_to(folder).as_posix()
    # FCS paths sometimes include a different Steam Workshop directory name.
    matching = [(p, rel) for rel, p in index.items()
                if rel.endswith("/" + normalized.casefold()) or rel == normalized.casefold()]
    if len(matching) == 1:
        path = matching[0][0]
        return path, path.relative_to(folder).as_posix()
    return None


def path_like(text):
    return Path(text.replace("\\", "/")).suffix.lower() in ASSET_EXT


def switch_paths(rec, owner, index, output_name, mod_stem, source_name=None):
    rec = copy.deepcopy(rec)
    copied = {}
    unresolved = []
    for field_index in (5, 6):
        replacements = []
        for key, value in rec.fields[field_index]:
            found = file_ref(value, owner, index, mod_stem)
            if found:
                actual, rel = found
                dest_rel = (f"_imported/{source_name}/{rel}" if source_name else rel)
                copied[dest_rel] = actual
                replacements.append((key, ".\\mods\\" + output_name + "\\" + dest_rel.replace("/", "\\")))
            else:
                replacements.append((key, value))
                if path_like(value) and (field_index == 6 or "mods" in value.lower()):
                    unresolved.append(f"{rec.name} [{key}] {value}")
        rec.fields[field_index] = replacements
    return rec, copied, unresolved


def write_output(source, target, roots, output_dir, bind_race_sid="", copy_source_assets=False):
    output_dir = Path(output_dir).resolve()
    if output_dir.exists():
        raise FormatError("输出文件夹已经存在，请选择一个新名字；不会覆盖现有模组")
    if output_dir.parent.name.casefold() != "mods":
        raise FormatError("输出目录必须直接位于 Kenshi\\mods 下面")
    if source.path.parent == target.path.parent:
        raise FormatError("源模组和目标模组不能位于同一目录")
    if output_dir == source.path.parent or output_dir == target.path.parent:
        raise FormatError("输出目录不能覆盖源或目标")
    if not roots:
        raise FormatError("请至少勾选一条源模组记录")
    chosen = choose_records(source, target, set(roots))
    src, dst = source.by_id, target.by_id
    output_name = output_dir.name
    src_index, dst_index = asset_index(source.path.parent), asset_index(target.path.parent)
    copy_map, unresolved, records, added = {}, [], [], []

    # Preserve target order, and merge selected CHANGED patches into target records.
    for original in target.records:
        patch = src.get(original.sid) if original.sid in chosen else None
        rec = merge_changed(original, patch) if patch else copy.deepcopy(original)
        rec, files, missing = switch_paths(rec, target.path.parent, dst_index, output_name,
                                           target.path.stem)
        copy_map.update(files)
        unresolved.extend(missing)
        if patch:
            # Paths from a source patch are source-owned even when the record is shared.
            overlay, files, missing = switch_paths(patch, source.path.parent, src_index,
                                                   output_name, source.path.stem, source.path.stem)
            for i in (5, 6):
                patch_keys = {k for k, _ in patch.fields[i]}
                values = dict(overlay.fields[i])
                rec.fields[i] = [(k, values[k] if k in patch_keys else v)
                                 for k, v in rec.fields[i]]
            copy_map.update(files)
            unresolved.extend(missing)
        records.append(rec)

    for original in source.records:
        if original.sid not in chosen or original.sid in dst:
            continue
        rec, files, missing = switch_paths(original, source.path.parent, src_index,
                                           output_name, source.path.stem, source.path.stem)
        records.append(rec)
        added.append(f"{KINDS.get(rec.kind, '类别 ' + str(rec.kind))}: {rec.name} ({rec.sid})")
        copy_map.update(files)
        unresolved.extend(missing)

    hairs = [r for r in records if r.sid in chosen and r.kind == 6]
    if bind_race_sid and hairs:
        race = next((r for r in records if r.sid == bind_race_sid), None)
        if not race or race.kind != 7:
            raise FormatError("目标人种不存在")
        records[records.index(race)] = bind_hairs(race, hairs)

    # Keep all unrelated target folder assets. Fail closed on a conflicting asset.
    for rel, source_file in copy_map.items():
        current = dst_index.get(rel.casefold())
        if current and current.resolve() != source_file.resolve() and current.read_bytes() != source_file.read_bytes():
            raise FormatError(f"资源文件路径冲突：{rel}")
    payload = target.prefix + pack("i", len(records)) + b"".join(r.to_bytes() for r in records)
    if copy_source_assets:
        for rel, path in src_index.items():
            original_rel = path.relative_to(source.path.parent).as_posix()
            copy_map.setdefault(f"_imported/{source.path.stem}/{original_rel}", path)

    temp = Path(tempfile.mkdtemp(prefix=".kenshi_import_", dir=output_dir.parent))
    try:
        # Preserve target resources not mentioned by an FCS filename field.
        for path in target.path.parent.rglob("*"):
            if path.is_file() and path.suffix.lower() != ".mod":
                rel = path.relative_to(target.path.parent)
                dest = temp / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
        for rel, path in copy_map.items():
            dest = temp / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
        mod_path = temp / (output_name + ".mod")
        mod_path.write_bytes(payload)
        check = load_mod(mod_path)
        if len(check.records) != len(records) or check.path.read_bytes() != payload:
            raise FormatError("输出模组未通过重新读取校验")
        report = {
            "source": str(source.path), "target": str(target.path),
            "selected_ids": list(roots), "imported_ids": sorted(chosen),
            "added_records": added, "total_records": len(records),
            "copied_source_files": sorted(p for p in copy_map if p.startswith("_imported/")),
            "possible_missing_files": sorted(set(unresolved)),
            "source_dependencies": source.dependencies,
            "source_references": source.references,
            "target_dependencies": target.dependencies,
            "note": "外部依赖、网格内部材质和 skeleton 链接需在 FCS 和游戏中核对。"
        }
        (temp / "导入报告.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.rename(output_dir)
        return output_dir, report
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Kenshi 模组选择性导入工具")
        self.geometry("1050x720")
        self.source = self.target = None
        self.src_path, self.dst_path, self.output_path = (tk.StringVar() for _ in range(3))
        self.search = tk.StringVar()
        self.kind = tk.StringVar(value="全部")
        self.race = tk.StringVar(value="不绑定发型")
        self.all_assets = tk.BooleanVar(value=False)
        self.checked = set()
        self._build()

    def _build(self):
        box = ttk.Frame(self, padding=12)
        box.pack(fill="both", expand=True)
        for line, (label, var, callback) in enumerate((
            ("mod2（来源）", self.src_path, self.pick_source),
            ("mod1（自己的）", self.dst_path, self.pick_target),
            ("新模组目录", self.output_path, self.pick_output))):
            ttk.Label(box, text=label, width=16).grid(row=line, column=0, sticky="w", pady=3)
            ttk.Entry(box, textvariable=var).grid(row=line, column=1, sticky="ew", pady=3)
            ttk.Button(box, text="浏览…", command=callback).grid(row=line, column=2, padx=5)
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text="输出应为 Kenshi\\mods\\新模组名；生成同名 .mod，原有 mod1 / mod2 不会修改。",
                  foreground="#4b5563").grid(row=3, column=0, columnspan=3, sticky="w", pady=4)
        top = ttk.Frame(box)
        top.grid(row=4, column=0, columnspan=3, sticky="ew", pady=5)
        ttk.Label(top, text="筛选").pack(side="left")
        ttk.Entry(top, textvariable=self.search, width=36).pack(side="left", padx=5)
        self.filter = ttk.Combobox(top, textvariable=self.kind, state="readonly", width=17)
        self.filter["values"] = ("全部",) + tuple(dict.fromkeys(KINDS.values())) + ("其他",)
        self.filter.pack(side="left", padx=6)
        ttk.Button(top, text="全选当前筛选", command=self.select_visible).pack(side="left", padx=3)
        ttk.Button(top, text="清空选择", command=self.clear).pack(side="left", padx=3)
        self.tree = ttk.Treeview(box, columns=("type", "state", "sid"), show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="选择 / 记录名称")
        self.tree.heading("type", text="类别")
        self.tree.heading("state", text="状态")
        self.tree.heading("sid", text="String ID")
        self.tree.column("#0", width=390)
        self.tree.column("type", width=120, stretch=False)
        self.tree.column("state", width=110, stretch=False)
        self.tree.column("sid", width=290)
        self.tree.grid(row=5, column=0, columnspan=3, sticky="nsew")
        scroller = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        scroller.grid(row=5, column=3, sticky="ns")
        self.tree.configure(yscrollcommand=scroller.set)
        box.rowconfigure(5, weight=1)
        self.tree.bind("<Double-1>", self.toggle)
        self.tree.bind("<space>", self.toggle)
        self.search.trace_add("write", lambda *_: self.refresh())
        self.filter.bind("<<ComboboxSelected>>", lambda *_: self.refresh())
        row = ttk.Frame(box)
        row.grid(row=6, column=0, columnspan=3, sticky="ew", pady=7)
        ttk.Label(row, text="把选中发型挂到目标人种：").pack(side="left")
        self.races = ttk.Combobox(row, textvariable=self.race, state="readonly", width=34)
        self.races["values"] = ("不绑定发型",)
        self.races.pack(side="left")
        ttk.Checkbutton(row, text="连源模组未直接引用的资源也复制（更完整、更占空间）",
                        variable=self.all_assets).pack(side="left", padx=18)
        ttk.Button(box, text="导入勾选记录、自动关联并生成新模组", command=self.run).grid(
            row=7, column=0, columnspan=3, sticky="ew", pady=5)
        self.status = tk.StringVar(value="请先选择 mod2 和 mod1 的 .mod 文件；双击记录可勾选。")
        ttk.Label(box, textvariable=self.status, wraplength=970).grid(row=8, column=0, columnspan=3, sticky="w")

    def pick_source(self):
        path = filedialog.askopenfilename(title="选择 mod2 的 .mod", filetypes=[("Kenshi mod", "*.mod")])
        if path:
            self.src_path.set(path)
            self.load()

    def pick_target(self):
        path = filedialog.askopenfilename(title="选择自己的 mod1 的 .mod", filetypes=[("Kenshi mod", "*.mod")])
        if path:
            self.dst_path.set(path)
            parent = Path(path).resolve().parent
            if parent.parent.name.casefold() == "mods":
                self.output_path.set(str(parent.parent / (Path(path).stem + "_merged")))
            self.load()

    def pick_output(self):
        parent = filedialog.askdirectory(title="选择 Kenshi 的 mods 文件夹")
        if parent:
            basename = (Path(self.dst_path.get()).stem or "MyMod") + "_merged"
            self.output_path.set(str(Path(parent) / basename))

    def load(self):
        try:
            self.source = load_mod(self.src_path.get()) if self.src_path.get() else None
            self.target = load_mod(self.dst_path.get()) if self.dst_path.get() else None
            self.checked.clear()
            self.refresh()
            if self.target:
                self.race_map = {f"{r.name} [{r.sid}]": r.sid for r in self.target.records if r.kind == 7}
                self.races["values"] = ("不绑定发型",) + tuple(self.race_map)
                self.race.set("不绑定发型")
            if self.source:
                self.status.set(f"已读取来源 {len(self.source.records)} 条记录；双击选择需要导入的内容。")
        except Exception as exc:
            self.source = self.target = None
            self.checked.clear()
            self.refresh()
            messagebox.showerror("读取失败", str(exc))

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        if not self.source:
            return
        query = self.search.get().casefold().strip()
        kind = self.kind.get()
        for rec in self.source.records:
            label = KINDS.get(rec.kind, "其他")
            if kind != "全部" and kind != label:
                continue
            if query and query not in (rec.name + " " + rec.sid + " " + label).casefold():
                continue
            state = "新增" if rec.datatype == -2147483646 else "修改/覆盖"
            self.tree.insert("", "end", iid=rec.sid, text=("☑ " if rec.sid in self.checked else "☐ ") + rec.name,
                             values=(label, state, rec.sid))
        self.status.set(f"当前显示 {len(self.tree.get_children())} 条；已选 {len(self.checked)} 条。")

    def toggle(self, _event=None):
        ids = self.tree.selection()
        if not ids:
            ids = (self.tree.identify_row(_event.y),) if _event else ()
        for sid in ids:
            if sid:
                self.checked.symmetric_difference_update({sid})
        self.refresh()

    def select_visible(self):
        self.checked.update(self.tree.get_children())
        self.refresh()

    def clear(self):
        self.checked.clear()
        self.refresh()

    def run(self):
        try:
            if not self.source or not self.target:
                raise FormatError("先选择并成功读取来源、目标两个 .mod")
            race_sid = getattr(self, "race_map", {}).get(self.race.get(), "")
            output, report = write_output(self.source, self.target, sorted(self.checked),
                                          self.output_path.get(), race_sid, self.all_assets.get())
            messagebox.showinfo("完成", f"已生成：{output}\n\n导入 {len(report['imported_ids'])} 条关联记录，"
                                f"其中新增 {len(report['added_records'])} 条；资源路径疑点 "
                                f"{len(report['possible_missing_files'])} 条。请看 导入报告.json，"
                                "并在 FCS 打开新模组核对建筑动作和人物外观。")
            self.status.set("生成完成：" + str(output))
        except Exception as exc:
            messagebox.showerror("未导入", str(exc))


if __name__ == "__main__":
    if "--check" in sys.argv:
        for item in sys.argv[2:]:
            mod = load_mod(item)
            print(f"{mod.path}: {len(mod.records)} records, lossless parse OK")
    else:
        App().mainloop()
