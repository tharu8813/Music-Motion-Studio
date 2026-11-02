"""Dedicated OpenAI account management; credentials never enter project data."""
import time
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout
from app.services.chatgpt_auth_service import ChatGPTAuthService
from app.services.lyrics_translation_service import TranslationError
from app.services.translation_worker import TranslationWorker
from app.dialogs.help_dialog import install_help_shortcut


class OpenAIAccountDialog(QDialog):
    def __init__(self, translator, parent=None, *, auth=None):
        super().__init__(parent)
        self.translator, self.auth = translator, auth or ChatGPTAuthService()
        install_help_shortcut(self, ("lyrics_editor", "lyrics_translation"), translator=translator)
        self.worker, self._closing = None, False
        self.setWindowTitle(self.tr_text("OpenAI 계정 관리", "OpenAI account"))
        self.setMinimumWidth(460)
        self.resize(540, 410)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(14)
        title = QLabel(self.windowTitle())
        title.setObjectName("dialogTitle")
        root.addWidget(title)
        note = QLabel(self.tr_text("브라우저에서 ChatGPT로 로그인합니다. 로그인 정보는 이 Windows 계정으로 암호화하며 앱을 다시 실행해도 유지됩니다.",
            "Sign in with ChatGPT in your browser. Credentials are encrypted for this Windows account and kept after restarting the app."))
        note.setWordWrap(True)
        note.setObjectName("mutedLabel")
        root.addWidget(note)
        form = QFormLayout()
        self.email, self.connection, self.permission, self.session = (QLabel() for _ in range(4))
        for label, ko, en in ((self.email, "계정", "Account"), (self.connection, "연결 상태", "Connection"),
                (self.permission, "번역 사용 권한", "Translation permission"), (self.session, "로그인 유지", "Session")):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            form.addRow(self.tr_text(ko, en), label)
        root.addLayout(form)
        actions = QHBoxLayout()
        self.sign_in = QPushButton("Continue with ChatGPT")
        self.sign_in.setProperty("primary", True)
        self.verify = QPushButton(self.tr_text("연결 확인", "Check connection"))
        self.sign_out = QPushButton(self.tr_text("로그아웃", "Sign out"))
        for button in (self.sign_in, self.verify, self.sign_out):
            button.setAutoDefault(False)
            actions.addWidget(button)
        root.addLayout(actions)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        root.addWidget(self.progress)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        root.addStretch()
        footer = QHBoxLayout()
        self.cancel_request = QPushButton(self.tr_text("요청 취소", "Cancel request"))
        self.cancel_request.hide()
        self.cancel_request.clicked.connect(self._cancel)
        footer.addWidget(self.cancel_request)
        footer.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        footer.addWidget(buttons)
        root.addLayout(footer)
        self.sign_in.clicked.connect(lambda: self._start("signin", lambda cancel, progress: self.auth.sign_in(cancel, remember=True)))
        self.verify.clicked.connect(lambda: self._start("verify", lambda cancel, progress: self.auth.models(cancel)))
        self.sign_out.clicked.connect(lambda: self._start("signout", lambda cancel, progress: self.auth.sign_out(cancel)))
        self.refresh()

    def tr_text(self, ko, en):
        return ko if self.translator.is_korean else en

    def refresh(self):
        try:
            profile = self.auth.profile()
        except TranslationError as error:
            profile = self.auth.credentials.load("chatgpt")
            self.status.setText(str(error))
        self.email.setText(profile.get("email") or self.tr_text("연결된 계정 없음", "No connected account"))
        connected = bool(profile.get("access_token"))
        renewable = bool(profile.get("refresh_token"))
        expired = float(profile.get("expires_at", 0)) <= time.time()
        self.connection.setText(self.tr_text("연결됨 · 필요 시 자동 갱신", "Connected · refreshes automatically") if connected and (renewable or not expired)
            else self.tr_text("다시 로그인 필요", "Sign in again") if connected else self.tr_text("로그인 안 됨", "Not signed in"))
        allowed = "chatgpt.tokens.use.direct" in profile.get("scope", "").split()
        self.permission.setText(self.tr_text("승인됨 · 계정의 사용 한도 적용", "Granted · account usage limits apply") if allowed
            else self.tr_text("로그인 후 구독 사용 승인 필요", "Sign in and authorize plan usage"))
        self.session.setText(self.tr_text("암호화 저장 · 로그아웃할 때 삭제", "Encrypted · removed when you sign out") if profile.get("remember")
            else self.tr_text("연결된 로그인 정보 없음", "No saved session"))
        self.verify.setEnabled(connected and self.worker is None)
        self.sign_out.setEnabled(bool(profile) and self.worker is None)

    def _start(self, operation, work):
        if self.worker:
            return
        self._operation = operation
        self.worker = TranslationWorker(work, self)
        self.worker.finished.connect(self._finished)
        for button in (self.sign_in, self.verify, self.sign_out):
            button.setEnabled(False)
        self.progress.show()
        self.cancel_request.show()
        self.status.setText(self.tr_text("브라우저에서 로그인을 완료하세요." if operation == "signin" else "처리 중…",
            "Complete sign-in in your browser." if operation == "signin" else "Working…"))
        self.worker.start()

    def _finished(self):
        worker, self.worker = self.worker, None
        self.progress.hide()
        self.cancel_request.hide()
        self.sign_in.setEnabled(True)
        self.refresh()
        self.status.setText(worker.error or (self.tr_text("취소했습니다.", "Cancelled.") if worker.cancel.is_set()
            else self.tr_text("로그아웃했습니다." if self._operation == "signout" else "계정 연결을 확인했습니다.",
                "Signed out." if self._operation == "signout" else "Account connection confirmed.")))
        worker.deleteLater()
        if self._closing:
            super().reject()

    def _cancel(self):
        if self.worker:
            self.worker.cancel.set()
            self.status.setText(self.tr_text("취소 중… 현재 요청이 끝나면 닫힙니다.", "Cancelling… waiting for the current request."))

    def done(self, result):
        if self.worker:
            self._closing = True
            self._cancel()
            return
        super().done(result)
