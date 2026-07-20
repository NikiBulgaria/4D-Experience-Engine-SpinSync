"""
question_editor.py — GAME FLOW editor.

Port of the '◈ GAME FLOW' section of VideoHardwareControllerEditor.cs:
root questions -> answers -> (chance %, effect, value, ⤵ skip next root,
▸ sub-question). Sub-questions open the same editor recursively in a dialog,
so the SerializeReference nesting from Unity is fully supported.
"""

from __future__ import annotations

from typing import Callable, List, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox,
                             QFrame, QHBoxLayout, QLabel, QLineEdit,
                             QListWidget, QPushButton, QSizePolicy, QStyle, QSpinBox, QSplitter,
                             QStackedWidget, QVBoxLayout, QWidget)

from config import (ANSWER_EFFECTS, GameAnswer, GameQuestion, QUESTION_MODES,
                    balance_weights, effective_shares,
                    VideoScenario, build_video_question)
from widgets import header

# provider returns the live playlist so combos and the auto-builder stay in sync
PlaylistProvider = Callable[[], List[VideoScenario]]

_EFFECT_LABELS = {
    "None": "None",
    "SetBreakTime": "Set Break Time (s)",
    "SetLoopCount": "Set Loop Count",
    "PlayVideoFromPlaylist": "Play Video From Playlist",
}


# ==========================================================================
class AnswerRow(QFrame):
    changed = pyqtSignal()
    delete_me = pyqtSignal(object)
    move_me = pyqtSignal(object, int)
    chance_edited = pyqtSignal(object)

    def __init__(self, answer: GameAnswer, playlist: PlaylistProvider,
                 parent=None):
        super().__init__(parent)
        self.answer = answer
        self.playlist = playlist
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet("QFrame{background:#1a1d22; border:1px solid #2a2e36;"
                           "border-radius:7px;}")

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.label_edit = QLineEdit(answer.label)
        self.label_edit.setPlaceholderText("Answer label (wheel slice text)")
        # Ignored width: this field gives up space first, so the controls to
        # its right stay on screen instead of running past the window edge.
        self.label_edit.setSizePolicy(QSizePolicy.Policy.Ignored,
                                      QSizePolicy.Policy.Fixed)
        self.label_edit.setMinimumWidth(90)
        self.label_edit.textChanged.connect(self._label_changed)
        top.addWidget(self.label_edit, 3)

        top.addWidget(QLabel("Chance %"))
        self.chance = QDoubleSpinBox()
        self.chance.setRange(0, 100)
        self.chance.setDecimals(2)
        self.chance.setMaximumWidth(96)
        self.chance.setValue(answer.chance_weight)
        self.chance.valueChanged.connect(self._chance_changed)
        top.addWidget(self.chance)
        self.odds_label = QLabel("")
        self.odds_label.setFixedWidth(74)
        self.odds_label.setToolTip("What the wheel will actually do with these "
                                   "numbers, after they are normalised.")
        self.odds_label.setStyleSheet("color:#35c5d0; font-weight:700;")
        top.addWidget(self.odds_label)

        # Standard icons instead of unicode glyphs: the arrows and the cross
        # rendered as blank bars in fonts that lack them, which made three
        # different buttons look identical.
        style = self.style()
        self.up_btn = QPushButton()
        self.up_btn.setIcon(style.standardIcon(
            QStyle.StandardPixmap.SP_ArrowUp))
        self.up_btn.setToolTip("Move this answer up (order on the wheel)")
        self.down_btn = QPushButton()
        self.down_btn.setIcon(style.standardIcon(
            QStyle.StandardPixmap.SP_ArrowDown))
        self.down_btn.setToolTip("Move this answer down")
        self.del_btn = QPushButton(" Delete")
        self.del_btn.setIcon(style.standardIcon(
            QStyle.StandardPixmap.SP_DialogDiscardButton))
        self.del_btn.setToolTip("Delete this answer")
        self.del_btn.setStyleSheet(
            "QPushButton{color:#ff8f8f; border:1px solid #6a2020;}"
            "QPushButton:hover{background:#5a1c1c; color:#fff;}")
        for b in (self.up_btn, self.down_btn):
            b.setFixedWidth(30)
        self.del_btn.setMaximumWidth(92)
        self.up_btn.clicked.connect(lambda: self.move_me.emit(self, -1))
        self.down_btn.clicked.connect(lambda: self.move_me.emit(self, +1))
        self.del_btn.clicked.connect(lambda: self.delete_me.emit(self))
        top.addWidget(self.up_btn)
        top.addWidget(self.down_btn)
        top.addWidget(self.del_btn)
        root.addLayout(top)

        mid = QHBoxLayout()
        mid.setSpacing(8)
        mid.addWidget(QLabel("Effect"))
        self.effect = QComboBox()
        for e in ANSWER_EFFECTS:
            self.effect.addItem(_EFFECT_LABELS[e], e)
        self.effect.setCurrentIndex(ANSWER_EFFECTS.index(answer.effect))
        self.effect.currentIndexChanged.connect(self._effect_changed)
        self.effect.setSizePolicy(QSizePolicy.Policy.Ignored,
                                  QSizePolicy.Policy.Fixed)
        self.effect.setMinimumWidth(110)
        mid.addWidget(self.effect, 2)

        self.value_stack = QStackedWidget()
        self.float_spin = QDoubleSpinBox()
        self.float_spin.setRange(0, 100000)
        self.float_spin.setDecimals(1)
        self.float_spin.setValue(answer.float_value)
        self.float_spin.valueChanged.connect(self._float_changed)
        self.video_combo = QComboBox()
        self.video_combo.currentIndexChanged.connect(self._video_changed)
        self.value_stack.addWidget(self.float_spin)
        self.value_stack.addWidget(self.video_combo)
        mid.addWidget(self.value_stack, 2)
        root.addLayout(mid)

        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        self.sync_check = QCheckBox("Use the video's name")
        self.sync_check.setToolTip(
            "Keep this slice labelled with whatever the playlist entry is "
            "called, so renaming a video updates the wheel automatically.")
        self.sync_check.setChecked(answer.sync_label_with_video)
        self.sync_check.toggled.connect(self._sync_changed)
        bottom.addWidget(self.sync_check)
        self.skip_check = QCheckBox("\u2935 Skip next root question")
        self.skip_check.setChecked(answer.skip_next_root)
        self.skip_check.toggled.connect(self._skip_changed)
        bottom.addWidget(self.skip_check)
        bottom.addStretch(1)
        self.sub_btn = QPushButton()
        self.sub_btn.clicked.connect(self._edit_sub)
        bottom.addWidget(self.sub_btn)
        root.addLayout(bottom)

        self._refresh_video_combo()
        self._refresh_effect_ui()
        self._refresh_sub_btn()

    # ---- write-through bindings -------------------------------------------
    def _label_changed(self, text: str):
        self.answer.label = text
        self.changed.emit()

    def _chance_changed(self, v: float):
        self.answer.chance_weight = v
        self.chance_edited.emit(self)
        self.changed.emit()

    def _float_changed(self, v: float):
        self.answer.float_value = v
        self.changed.emit()

    def _video_changed(self, idx: int):
        if idx >= 0:
            self.answer.playlist_index = idx
            self._refresh_effect_ui()
            self.changed.emit()

    def set_odds(self, share: float):
        self.odds_label.setText(f"\u2192 {share:0.1f}%")

    def refresh_chance(self):
        if abs(self.chance.value() - self.answer.chance_weight) > 1e-6:
            self.chance.blockSignals(True)
            self.chance.setValue(self.answer.chance_weight)
            self.chance.blockSignals(False)

    def set_position(self, index: int, total: int):
        """Grey out the arrows at the ends so a dead button is obviously dead
        rather than looking broken."""
        self.up_btn.setEnabled(index > 0)
        self.down_btn.setEnabled(index < total - 1)
        self.up_btn.setToolTip("Move this answer up"
                               if index > 0 else "Already first")
        self.down_btn.setToolTip("Move this answer down"
                                 if index < total - 1 else "Already last")

    def _skip_changed(self, on: bool):
        self.answer.skip_next_root = on
        self.changed.emit()

    def _sync_changed(self, on: bool):
        self.answer.sync_label_with_video = on
        self._refresh_effect_ui()
        self.changed.emit()

    def _effect_changed(self, idx: int):
        self.answer.effect = self.effect.itemData(idx)
        self._refresh_effect_ui()
        self.changed.emit()

    def _refresh_effect_ui(self):
        eff = self.answer.effect
        plays_video = eff == "PlayVideoFromPlaylist"
        self.sync_check.setVisible(plays_video)
        if plays_video and self.answer.sync_label_with_video:
            shown = self.answer.resolve_label(self.playlist())
            self.label_edit.setEnabled(False)
            self.label_edit.setPlaceholderText(shown)
            self.label_edit.setToolTip(
                f"The wheel shows '{shown}' (the playlist name). Untick "
                f"'Use the video's name' to type a custom label.")
        else:
            self.label_edit.setEnabled(True)
            self.label_edit.setToolTip("")
        if plays_video:
            self._refresh_video_combo()
            self.value_stack.setCurrentIndex(1)
            self.value_stack.setEnabled(True)
        else:
            self.value_stack.setCurrentIndex(0)
            self.value_stack.setEnabled(eff in ("SetBreakTime", "SetLoopCount"))
            self.float_spin.setSuffix(
                " s" if eff == "SetBreakTime"
                else (" loops" if eff == "SetLoopCount" else ""))

    def _refresh_video_combo(self):
        labels = [s.wheel_label for s in self.playlist()]
        self.video_combo.blockSignals(True)
        self.video_combo.clear()
        for i, lbl in enumerate(labels):
            self.video_combo.addItem(f"[{i}] {lbl}")
        if 0 <= self.answer.playlist_index < len(labels):
            self.video_combo.setCurrentIndex(self.answer.playlist_index)
        self.video_combo.blockSignals(False)

    # ---- sub-question -------------------------------------------------------
    def _refresh_sub_btn(self):
        if self.answer.sub_question is not None:
            self.sub_btn.setText("\u25b8 Sub-Question \u2713")
            self.sub_btn.setStyleSheet("QPushButton{color:#35c5d0;}")
        else:
            self.sub_btn.setText("\u25b8 Add Sub-Question")
            self.sub_btn.setStyleSheet("")

    def _edit_sub(self):
        if self.answer.sub_question is None:
            self.answer.sub_question = GameQuestion(text="New Sub-Question",
                                                    log_name="SubQ")
        dlg = SubQuestionDialog(self.answer, self.playlist, self)
        dlg.changed.connect(self.changed.emit)
        dlg.exec()
        self._refresh_sub_btn()
        self.changed.emit()


# ==========================================================================
class QuestionForm(QWidget):
    """Edits one GameQuestion (used for roots and, recursively, sub-questions)."""

    changed = pyqtSignal()

    def __init__(self, playlist: PlaylistProvider, parent=None):
        super().__init__(parent)
        self.playlist = playlist
        self.question: Optional[GameQuestion] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        r1 = QHBoxLayout()
        r1.setSpacing(8)
        r1.addWidget(QLabel("Question Text"))
        self.text_edit = QLineEdit()
        self.text_edit.textChanged.connect(self._text_changed)
        r1.addWidget(self.text_edit, 1)
        root.addLayout(r1)

        r2 = QHBoxLayout()
        r2.addWidget(QLabel("Log Name"))
        self.log_edit = QLineEdit()
        self.log_edit.setMaximumWidth(140)
        self.log_edit.textChanged.connect(self._log_changed)
        r2.addWidget(self.log_edit)
        r2.addWidget(QLabel("Mode"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(QUESTION_MODES)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        r2.addWidget(self.mode_combo)
        r2.addWidget(QLabel("Max Picks"))
        self.max_picks = QSpinBox()
        self.max_picks.setRange(0, 999)
        self.max_picks.valueChanged.connect(self._picks_changed)
        r2.addWidget(self.max_picks)
        r2.addStretch(1)
        root.addLayout(r2)

        head_row = QHBoxLayout()
        head_row.setSpacing(12)
        self.answers_header = header("ANSWERS (0)")
        head_row.addWidget(self.answers_header)
        head_row.addStretch(1)
        self.balance_check = QCheckBox("Balance to 100%")
        self.balance_check.setChecked(True)
        self.balance_check.setToolTip(
            "Typing a chance rescales the others so the set adds up to 100, "
            "keeping their relative odds. Untick to enter raw weights; the "
            "arrow beside each still shows the real odds.")
        self.balance_check.toggled.connect(self._refresh_odds)
        head_row.addWidget(self.balance_check)
        normalise = QPushButton("Normalise")
        normalise.setMaximumWidth(110)
        normalise.setToolTip("Rescale every chance so they total 100%.")
        normalise.clicked.connect(self._normalise)
        head_row.addWidget(normalise)
        root.addLayout(head_row)
        self.answers_box = QVBoxLayout()
        self.answers_box.setContentsMargins(0, 0, 0, 0)
        self.answers_box.setSpacing(8)
        holder = QWidget()
        holder.setLayout(self.answers_box)
        root.addWidget(holder)

        add = QPushButton("\uff0b  ADD ANSWER")
        add.setMinimumHeight(34)
        add.setToolTip("Add another slice to this question's wheel.")
        add.clicked.connect(self._add_answer)
        root.addSpacing(4)
        root.addWidget(add)
        root.addStretch(1)

    # ---- load / bind ---------------------------------------------------------
    def set_question(self, q: Optional[GameQuestion]):
        self.question = q
        enabled = q is not None
        self.setEnabled(enabled)
        for w in (self.text_edit, self.log_edit, self.mode_combo,
                  self.max_picks):
            w.blockSignals(True)
        if q is not None:
            self.text_edit.setText(q.text)
            self.log_edit.setText(q.log_name)
            self.mode_combo.setCurrentIndex(
                QUESTION_MODES.index(q.mode) if q.mode in QUESTION_MODES else 0)
            self.max_picks.setValue(q.max_picks)
        else:
            self.text_edit.clear()
            self.log_edit.clear()
        for w in (self.text_edit, self.log_edit, self.mode_combo,
                  self.max_picks):
            w.blockSignals(False)
        self._rebuild_answers()

    def _rebuild_answers(self):
        while self.answers_box.count():
            item = self.answers_box.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        if self.question is None:
            self.answers_header.setText("ANSWERS (0)")
            return
        self._rows = []
        total = len(self.question.answers)
        for index, a in enumerate(self.question.answers):
            row = AnswerRow(a, self.playlist)
            row.changed.connect(self.changed.emit)
            row.delete_me.connect(self._delete_answer)
            row.move_me.connect(self._move_answer)
            row.set_position(index, total)
            row.chance_edited.connect(self._chance_edited)
            self._rows.append(row)
            self.answers_box.addWidget(row)
        self.answers_header.setText(f"ANSWERS ({len(self.question.answers)})")
        self._refresh_odds()

    def _normalise(self):
        if self.question is None or not self.question.answers:
            return
        shares = effective_shares([a.chance_weight
                                   for a in self.question.answers])
        for answer, share in zip(self.question.answers, shares):
            answer.chance_weight = round(share, 2)
        for row in self._rows:
            row.refresh_chance()
        self._refresh_odds()
        self.changed.emit()

    def _chance_edited(self, row):
        """Unity-style: fix the number you typed, share the rest out."""
        if self.question is None or not self.balance_check.isChecked():
            self._refresh_odds()
            return
        try:
            index = self.question.answers.index(row.answer)
        except ValueError:
            return
        balance_weights(self.question.answers, index)
        for other in self._rows:
            if other is not row:
                other.refresh_chance()
        self._refresh_odds()

    def _refresh_odds(self):
        if self.question is None:
            return
        shares = effective_shares([a.chance_weight
                                   for a in self.question.answers])
        for row, share in zip(self._rows, shares):
            row.set_odds(share)

    # ---- edits ------------------------------------------------------------------
    def _text_changed(self, t: str):
        if self.question:
            self.question.text = t
            self.changed.emit()

    def _log_changed(self, t: str):
        if self.question:
            self.question.log_name = t
            self.changed.emit()

    def _mode_changed(self, idx: int):
        if self.question:
            self.question.mode = QUESTION_MODES[idx]
            self.changed.emit()

    def _picks_changed(self, v: int):
        if self.question:
            self.question.max_picks = v
            self.changed.emit()

    def _add_answer(self):
        if self.question is None:
            return
        self.question.answers.append(GameAnswer(label="Option"))
        self._rebuild_answers()
        self.changed.emit()

    def _delete_answer(self, row: AnswerRow):
        if self.question and row.answer in self.question.answers:
            self.question.answers.remove(row.answer)
            self._rebuild_answers()
            self.changed.emit()

    def _move_answer(self, row: AnswerRow, delta: int):
        if not self.question:
            return
        lst = self.question.answers
        i = lst.index(row.answer)
        j = i + delta
        if 0 <= j < len(lst):
            lst[i], lst[j] = lst[j], lst[i]
            self._rebuild_answers()
            self.changed.emit()


class SubQuestionDialog(QDialog):
    changed = pyqtSignal()

    def __init__(self, answer: GameAnswer, playlist: PlaylistProvider,
                 parent=None):
        super().__init__(parent)
        self.answer = answer
        self.setWindowTitle(f"Sub-Question of '{answer.label}'")
        self.setMinimumSize(560, 420)
        root = QVBoxLayout(self)
        form = QuestionForm(playlist)
        form.set_question(answer.sub_question)
        form.changed.connect(self.changed.emit)
        root.addWidget(form, 1)
        row = QHBoxLayout()
        rm = QPushButton("Remove Sub-Question")
        rm.setStyleSheet("QPushButton{color:#ff5c5c;}")
        rm.clicked.connect(self._remove)
        row.addWidget(rm)
        row.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        root.addLayout(row)

    def _remove(self):
        self.answer.sub_question = None
        self.changed.emit()
        self.accept()


# ==========================================================================
class GameFlowEditor(QWidget):
    """The whole '◈ GAME FLOW' tab: root question list + form."""

    changed = pyqtSignal()

    def __init__(self, questions: List[GameQuestion],
                 playlist: PlaylistProvider, parent=None):
        super().__init__(parent)
        self.questions = questions
        self.playlist = playlist

        root = QVBoxLayout(self)
        root.addWidget(header("\u25c8  GAME FLOW"))

        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        self.qlist = QListWidget()
        self.qlist.currentRowChanged.connect(self._select)
        lv.addWidget(self.qlist, 1)

        btns = QHBoxLayout()
        add = QPushButton("+ Question")
        style = self.style()
        rem = QPushButton(" Delete")
        rem.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_DialogDiscardButton))
        rem.setToolTip("Delete this question")
        rem.setStyleSheet("QPushButton{color:#ff8f8f;}")
        up = QPushButton()
        up.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_ArrowUp))
        up.setToolTip("Move this question earlier")
        dn = QPushButton()
        dn.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_ArrowDown))
        dn.setToolTip("Move this question later")
        for b in (rem, up, dn):
            b.setFixedWidth(30)
        add.clicked.connect(self._add)
        rem.clicked.connect(self._remove)
        up.clicked.connect(lambda: self._move(-1))
        dn.clicked.connect(lambda: self._move(+1))
        btns.addWidget(add)
        btns.addWidget(up)
        btns.addWidget(dn)
        btns.addWidget(rem)
        lv.addLayout(btns)

        auto = QPushButton("\u2699 Auto-build 'WHICH VIDEO?' question")
        auto.setToolTip("Generates a question from the playlist using each "
                        "video's Skip / Force / Custom % flags.")
        auto.clicked.connect(self._auto_build)
        lv.addWidget(auto)

        split.addWidget(left)
        self.form = QuestionForm(playlist)
        self.form.changed.connect(self._form_changed)
        split.addWidget(self.form)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 2)
        root.addWidget(split, 1)

        hint = QLabel("The flow runs top to bottom. Order matters: the 'no "
                      "break' quirk fires on root question #2, and loops "
                      "restart from question #1 (or the question that picked "
                      "the video).")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#7f8694; font-size:11px;")
        root.addWidget(hint)

        self.refresh_list(select=0)

    # ---- list management ------------------------------------------------------
    def refresh_list(self, select: Optional[int] = None):
        cur = self.qlist.currentRow() if select is None else select
        self.qlist.blockSignals(True)
        self.qlist.clear()
        for i, q in enumerate(self.questions):
            self.qlist.addItem(f"Q{i} \u00b7 {q.text}")
        self.qlist.blockSignals(False)
        if self.questions:
            cur = max(0, min(cur if cur is not None else 0,
                             len(self.questions) - 1))
            self.qlist.setCurrentRow(cur)
            self.form.set_question(self.questions[cur])
        else:
            self.form.set_question(None)

    def _select(self, row: int):
        if 0 <= row < len(self.questions):
            self.form.set_question(self.questions[row])
        else:
            self.form.set_question(None)

    def _form_changed(self):
        row = self.qlist.currentRow()
        if 0 <= row < len(self.questions):
            self.qlist.item(row).setText(
                f"Q{row} \u00b7 {self.questions[row].text}")
        self.changed.emit()

    def _add(self):
        self.questions.append(GameQuestion(text="New Question",
                                           log_name=f"Q{len(self.questions)}"))
        self.refresh_list(select=len(self.questions) - 1)
        self.changed.emit()

    def _remove(self):
        row = self.qlist.currentRow()
        if 0 <= row < len(self.questions):
            del self.questions[row]
            self.refresh_list(select=row - 1 if row > 0 else 0)
            self.changed.emit()

    def _move(self, delta: int):
        row = self.qlist.currentRow()
        j = row + delta
        if 0 <= row < len(self.questions) and 0 <= j < len(self.questions):
            self.questions[row], self.questions[j] = (self.questions[j],
                                                      self.questions[row])
            self.refresh_list(select=j)
            self.changed.emit()

    def _auto_build(self):
        self.questions.append(build_video_question(self.playlist()))
        self.refresh_list(select=len(self.questions) - 1)
        self.changed.emit()
