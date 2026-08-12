"""Тесты maintenance-прохода heal_leaked_markup: чистка утёкшей разметки вызова
в 208 живых статьях (замер 2026-07-27). Хвост «…</content> + блоки полей +
</invoke>» вырезается, содержимое полей НЕ выбрасывается: session_summary/
open_questions переносятся прозой, tags сливаются в шапку. Якорь — только
строка, оканчивающаяся </content>; цитаты в середине строк и fenced-код не
трогаются (статьи про сам баг остаются целыми)."""
import memory_compiler.maintenance as m


def test_full_tail_variant_a_moved_to_prose():
    text = ("# Статья\n\n**Теги:** t\n\n## Записи\n\n### 2026-05-20 10:00\n"
            "Содержательный текст записи.</content>\n"
            "<session_summary>ИТОГ_МАРКЕР найден баг и починен</session_summary>\n"
            "<open_questions>ВОПРОС_МАРКЕР остался хвост</open_questions>\n"
            "</invoke>\n"
            "\n"
            "- [бэклинк](../memory-compiler/_reflections.md) (2026-05-18)\n")
    fixed, stats = m.heal_leaked_call_text(text)
    for junk in ("</content>", "<session_summary>", "<open_questions>", "</invoke>"):
        assert junk not in fixed
    assert "Содержательный текст записи." in fixed
    assert "Итог сессии: ИТОГ_МАРКЕР найден баг и починен" in fixed
    assert "Открытые вопросы: ВОПРОС_МАРКЕР остался хвост" in fixed
    assert "- [бэклинк](../memory-compiler/_reflections.md) (2026-05-18)" in fixed


def test_variant_b_tags_merged_into_header():
    text = ("# Статья\n\n**Теги:** старый\n\n## Записи\n\n### 2026-05-20 10:00\n"
            "Текст записи про пагинацию.</content>\n"
            '<parameter name="tags">["новый", "старый"]</parameter>\n'
            "</invoke>\n")
    fixed, _ = m.heal_leaked_call_text(text)
    assert "</content>" not in fixed and "<parameter" not in fixed
    assert "**Теги:** старый, новый\n" in fixed


def test_bare_content_suffix_stripped():
    text = "# С\n\n## Записи\n\n### 2026-05-20 10:00\nтекст урока.</content>\n"
    fixed, stats = m.heal_leaked_call_text(text)
    assert "текст урока.\n" in fixed and "</content>" not in fixed


def test_prose_mentions_untouched():
    text = ("# Диагноз\n\n## Записи\n\n### 2026-07-27 12:00\n"
            "Мусор вида «…текст.</content> + <session_summary>…</session_summary> + "
            "</invoke>» поражает 208 статей; лечится guard'ом.\n")
    fixed, stats = m.heal_leaked_call_text(text)
    assert fixed == text
    assert stats["anchors"] == 0 and stats["invokes"] == 0


def test_code_fence_untouched():
    text = ("# Дока\n\n## Записи\n\n### 2026-07-27 12:00\n"
            "Пример порчи:\n\n```\nстрока примера.</content>\n</invoke>\n```\n"
            "и обычный текст.\n")
    fixed, _ = m.heal_leaked_call_text(text)
    assert fixed == text


def test_lone_invoke_line_removed():
    text = "# С\n\n## Записи\n\n### 2026-05-20 10:00\nтекст.\n</invoke>\n\nдалее.\n"
    fixed, stats = m.heal_leaked_call_text(text)
    assert "</invoke>" not in fixed
    assert "текст.\n" in fixed and "далее.\n" in fixed
    assert stats["invokes"] == 1


def test_orphan_field_block_reported_not_touched():
    text = ("# С\n\n## Записи\n\n### 2026-05-20 10:00\nтекст.\n"
            "<session_summary>сирота без якоря</session_summary>\n")
    fixed, stats = m.heal_leaked_call_text(text)
    assert fixed == text
    assert stats["suspicious"] == 1


def test_secret_enc_line_suffix_stripped_enc_intact():
    text = ("# Секрет\n\n**Теги:** secret\n\n## Записи\n\n### 2026-05-20 10:00\n"
            "ENC:AbCdEf012345==</content>\n</invoke>\n")
    fixed, _ = m.heal_leaked_call_text(text)
    assert "ENC:AbCdEf012345==\n" in fixed
    assert "</content>" not in fixed and "</invoke>" not in fixed


def test_tags_without_header_line_become_prose():
    text = ("# Служебный\n\n## Записи\n\n### 2026-05-20 10:00\n"
            "текст.</content>\n"
            '<tags>["а", "б"]</tags>\n'
            "</invoke>\n")
    fixed, _ = m.heal_leaked_call_text(text)
    assert "<tags>" not in fixed
    assert "Теги: а, б" in fixed


def test_multiline_session_summary_collected():
    text = ("# С\n\n## Записи\n\n### 2026-05-20 10:00\n"
            "текст.</content>\n"
            "<session_summary>первая строка\n"
            "вторая строка итога</session_summary>\n"
            "</invoke>\n")
    fixed, _ = m.heal_leaked_call_text(text)
    assert "<session_summary>" not in fixed
    assert "Итог сессии: первая строка\nвторая строка итога" in fixed


def test_double_tail_tags_not_duplicated():
    """Статья с ДВУМЯ одинаковыми хвостами (задвоенные записи, issue #2):
    теги в шапку сливаются без дублей. Пойман на живом dry-run:
    1c/sqns_exchange_api_v2 получил каждый тег дважды."""
    tail = ('текст.</content>\n<parameter name="tags">["sqns", "api"]</parameter>\n</invoke>\n')
    text = ("# С\n\n**Теги:** api, старый\n\n## Записи\n\n"
            "### 2026-05-20 10:00\n" + tail + "\n### 2026-05-21 10:00\n" + tail)
    fixed, _ = m.heal_leaked_call_text(text)
    assert "**Теги:** api, старый, sqns\n" in fixed
    assert fixed.count("sqns") == 1


def test_open_tail_without_anchor_healed(knowledge_dir=None):
    """Форма БЕЗ якоря '</content>' (v1.54.3): параметр закрыт корректно, а следом
    с новой строки въехал незакрытый '<parameter name="tags">'. 216 живых статей."""
    text = ("# С\n\n**Теги:** старый\n\n## Записи\n\n### 2026-08-11 10:00\n"
            "Реле приедут первыми, очередь работ задаёт координатор.\n"
            '<parameter name="tags">["roadmap", "zigbee"]\n')
    fixed, stats = m.heal_leaked_call_text(text)
    assert "<parameter" not in fixed
    assert "Реле приедут первыми, очередь работ задаёт координатор." in fixed
    assert "**Теги:** старый, roadmap, zigbee\n" in fixed
    assert stats["open_tails"] == 1


def test_open_tail_mixed_closed_and_unclosed():
    """Живой случай: предыдущее поле закрыто СВОИМ тегом, последнее не закрыто вовсе."""
    text = ("# С\n\n## Записи\n\n### 2026-08-11 10:00\n"
            "Суть решения.\n"
            '<parameter name="open_questions">Осталось выбрать реле.</open_questions>\n'
            '<parameter name="tags">["zigbee"]\n')
    fixed, _ = m.heal_leaked_call_text(text)
    assert "<parameter" not in fixed and "</open_questions>" not in fixed
    assert "Открытые вопросы: Осталось выбрать реле." in fixed
    assert "Теги: zigbee" in fixed


def test_open_tail_does_not_swallow_text_up_to_later_close(knowledge_dir=None):
    """Незакрытый блок не должен «дотянуться» до чужого закрытия ниже по статье:
    без границы он съел бы весь абзац между ними."""
    text = ("# С\n\n## Записи\n\n### 2026-08-11 10:00\n"
            'Текст.\n<parameter name="tags">["a"]\n\n'
            "СОДЕРЖАТЕЛЬНЫЙ_АБЗАЦ который нельзя терять.\n"
            "конец записи</parameter>\n")
    fixed, _ = m.heal_leaked_call_text(text)
    assert "СОДЕРЖАТЕЛЬНЫЙ_АБЗАЦ который нельзя терять." in fixed


def test_standalone_equivalent_to_canonical():
    """scripts/heal_leaked_standalone.py (запуск на NAS, python3.8 без пакета) —
    копия heal_leaked_call_text. Меняешь грамматику в одном — правь второй."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "heal_standalone",
        Path(__file__).resolve().parent.parent / "scripts" / "heal_leaked_standalone.py")
    st = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(st)
    cases = [
        ("# С\n\n**Теги:** старый\n\n## Записи\n\n### 2026-05-20 10:00\n"
         "Текст.</content>\n<session_summary>итог сессии</session_summary>\n"
         '<parameter name="tags">["новый"]</parameter>\n</invoke>\n\n- [линк](../a.md)\n'),
        "# С\n\n## Записи\n\n### 2026-05-20 10:00\nтекст.</content>\n",
        ("# С\n\n## Записи\n\n### x\nПроза с «…</content> + <session_summary>…"
         "</session_summary>» в середине строки.\n"),
        "# С\n\n## Записи\n\n```\nкод.</content>\n</invoke>\n```\nтекст.\n",
        "# С\n\n## Записи\n\n### x\nтекст.\n</invoke>\n\nдалее.\n",
        "# С\n\n## Записи\n\n### x\nтекст.\n<session_summary>сирота</session_summary>\n",
        ("# С\n\n## Записи\n\n### x\nENC:AbC==</content>\n"
         '<tags>["а", "б"]</tags>\n</invoke>\n'),
        ("# С\n\n## Записи\n\n### x\nтекст.</content>\n"
         "<open_questions>вопрос\nна две строки</open_questions>\n</invoke>\n"),
        ("# С\n\n## Записи\n\n### x\nтекст.</content>\n<tags>[не json</tags>\n</invoke>\n"),
        # v1.54.3: хвост БЕЗ якоря — незакрытый блок и смешанная форма
        ("# С\n\n**Теги:** старый\n\n## Записи\n\n### x\nтекст.\n"
         '<parameter name="tags">["новый"]\n'),
        ("# С\n\n## Записи\n\n### x\nтекст.\n"
         '<parameter name="open_questions">вопрос</open_questions>\n'
         '<parameter name="tags">["t"]\n'),
        ("# С\n\n## Записи\n\n### x\nтекст.\n"
         '<parameter name="tags">["a"]\n\nабзац.\nхвост</parameter>\n'),
    ]
    for text in cases:
        assert st.heal_leaked_call_text(text) == m.heal_leaked_call_text(text), text[:60]


def test_walker_dry_run_apply_and_archive_untouched(knowledge_dir, monkeypatch):
    monkeypatch.setattr(m, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(m, "PROJECTS", ["testproj"])
    monkeypatch.setattr(m, "git_commit", lambda msg: None)

    sick = ("# С\n\n## Записи\n\n### 2026-05-20 10:00\n"
            "текст.</content>\n</invoke>\n")
    art = knowledge_dir / "testproj" / "sick.md"
    art.write_text(sick, encoding="utf-8")
    daily = knowledge_dir / "daily"
    daily.mkdir(exist_ok=True)
    dlog = daily / "2026-07-27.md"
    dlog.write_text(sick, encoding="utf-8")
    arch = daily / "archive"
    arch.mkdir(exist_ok=True)
    afile = arch / "2026-04-20.md"
    afile.write_text(sick, encoding="utf-8")

    touched, stats = m.heal_leaked_markup(dry_run=True)
    assert touched == 2
    assert art.read_text(encoding="utf-8") == sick          # dry-run не пишет

    touched, stats = m.heal_leaked_markup()
    assert touched == 2
    assert "</content>" not in art.read_text(encoding="utf-8")
    assert "</content>" not in dlog.read_text(encoding="utf-8")
    assert afile.read_text(encoding="utf-8") == sick        # архив ВНЕ прохода
