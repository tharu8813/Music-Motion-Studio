# Music Motion Studio Windows 패키징 가이드

Playlist Canvas에서 제품 이름을 변경했습니다. `setup.iss`의 기존 AppId GUID와 `.pvsproj`의 `PlaylistCanvas.Project` 연결 식별자는 유지합니다. 업그레이드는 이전 설치 경로를 재사용하고 실행 파일·시작 메뉴·기존 바탕 화면 바로가기를 새 이름으로 교체합니다. 내부 데이터 폴더 `%LOCALAPPDATA%\PlaylistCanvas`는 FFmpeg·프리셋·언어팩을 계속 찾을 수 있도록 유지합니다. Qt 설정과 복구 파일은 새 이름으로 첫 실행할 때 이전됩니다. 최신 앱의 자동 업데이트와 릴리즈 배포는 `tharu8813/Music-Motion-Studio` 저장소에서 처리하고, `tharu8813/Playlist-Canvas`는 구버전 사용자용 릴리즈 배포 저장소로 별도 유지합니다. `LICENSE.txt` 원문은 변경하지 않습니다.

아이콘 원본은 `app/resources/app_icon.svg`와 `project_file_icon.svg`입니다. `python tools/generate_app_icon.py`로 PNG와 16~256px의 10개 크기가 포함된 ICO를 함께 재생성합니다.

이 앱은 Windows 11 기준으로 PyInstaller의 **one-folder** 배포를 권장합니다. PySide6/Qt 플러그인을 안정적으로 포함하고, FFmpeg는 처음부터 번들에 넣지 않고 사용자가 앱 안에서 검증된 배포본을 내려받도록 구성되어 있습니다.

> 저장소 루트의 `ffmpeg` 폴더는 개발 및 로컬 렌더링 테스트용입니다. PyInstaller 배포본에는 포함되지 않습니다. 배포 폴더에 이 디렉터리를 수동으로 복사하지 마세요.

## 1. 빌드 환경 준비

공식 개발·배포 기준은 **Python 3.14.10, Windows x64 일반 빌드(GIL 사용)**입니다. 버전은 `.python-version`에 고정하며 Windows CI도 이 파일을 읽습니다. Python 3.15는 ONNX Runtime의 Windows 패키지가 아직 없어 전체 AutoMix 기능을 제공하는 배포 기준에서 제외합니다.

프로젝트 루트에서 Python 설치 관리자로 런타임을 설치한 뒤 가상 환경을 만듭니다. 다른 Python으로 만든 `.venv`가 있다면 먼저 이름을 바꿔 보관하고 새로 만드세요.

```powershell
py install 3.14.10
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-lock.txt
python -m pip install -r requirements-packaging.txt
```

PowerShell에서 활성화가 차단되면 활성화 없이 `.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt`로 설치하고 `.\.venv\Scripts\python.exe main.py`로 실행할 수 있습니다. VS Code에서는 **Python: Select Interpreter**에서 `.venv\Scripts\python.exe`를 선택하세요.

설치본에 들어가는 AutoMix 분석기:

- **Beat This! (ONNX)**: 기본 박자·다운비트 분석기. PyTorch 없이 `onnxruntime`(약 14 MB)으로 int8 양자화 모델(`app/automix/analysis/models/beat_this_final0_int8.onnx`, 약 23 MB, MIT)을 실행합니다. 실제 팝 31곡에서 librosa의 박자 일치도는 F 0.64였고 템포 옥타브 오류가 흔했습니다. ONNX 모델은 PyTorch 원본과 F 0.998(박자) / 0.994(다운비트)로 일치합니다.
- **Open-Unmix `umxhq` (ONNX)**: 보컬 구간 감지(`umxhq_vocals_int8.onnx`, 약 9 MB, MIT, 곡당 약 1.7초). 곡의 앞뒤 45초만 분리해 노래하는 구간에서는 믹스하지 않습니다. `umxl`은 가중치가 비상업 전용이라 쓰지 않습니다.
- 두 모델은 `tools/export_automix_onnx.py`로 다시 만듭니다.
- **YAMNet (ONNX)**: 구간별 음악 장르·분위기 분석(`yamnet.onnx`, 약 16 MB, Apache 2.0). 기존 ONNX Runtime으로 실행합니다. 고정 다운로드 버전, 체크섬, 라벨의 출처는 모델 폴더의 `YAMNET_NOTICE.md`에 있습니다.
- **librosa**: 키·에너지 분석, ONNX를 쓸 수 없을 때의 대체 박자 분석.
- **Sonara**: 곡 구조 분석(약 2 MB Rust 확장).

`playlist_canvas.spec`은 이 패키지나 모델 파일이 하나라도 없으면 빌드를 중단합니다(1.2.0.6은 빌드 환경에 librosa가 없어 AutoMix 분석이 빠진 채 배포되었습니다).

PyTorch 기반 분석기(PyTorch판 **Beat This!**, **Demucs** 보컬 분리)는 설치본에 넣지 않습니다. 설치본이 약 1 GB로 커지고 첫 분석 동안 CPU를 크게 점유했기 때문입니다. 빌드 환경에 설치돼 있어도 spec의 `excludes`로 제외됩니다. 코드(`app/automix/analysis/beat_this.py`, `vocals.py`)는 청감 평가 도구 등에서 명시적으로 요청할 때만 쓰입니다.

## 2. Windows 배포본 빌드

```powershell
pyinstaller --noconfirm --clean playlist_canvas.spec
```

완성 파일은 다음 폴더에 생성됩니다.

```text
dist\Music Motion Studio\Music Motion Studio.exe
```

`dist\Music Motion Studio` 폴더 전체가 Inno Setup 설치 프로그램에 포함되어야 합니다. 실행 파일만 따로 복사하면 Qt 및 Python 런타임 파일이 없어 실행되지 않습니다.

> **주의:** `build\playlist_canvas\Music Motion Studio.exe`는 PyInstaller가 조립 중에 만드는 중간 파일입니다. 이 파일은 `_internal\python314.dll`이 함께 배치되지 않으므로 실행하거나 배포하면 안 됩니다. 반드시 `dist\Music Motion Studio` 폴더 안의 EXE를 사용하세요.

공식 배포 빌드는 `requirements-lock.txt`의 고정 버전을 사용합니다. 의존성을 갱신할 때는 로컬 테스트와 Windows CI가 모두 통과한 뒤 잠금 파일을 함께 갱신합니다.

## 3. Setup 설치 파일 빌드

Inno Setup 6을 설치한 뒤 프로젝트 루트에서 다음 명령을 실행합니다.

```powershell
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" setup.iss
```

`setup.iss`는 `dist\Music Motion Studio` 폴더 전체를 설치 원본으로 사용합니다. 따라서 PyInstaller 빌드가 먼저 성공해야 합니다.

설치는 관리자 권한을 요청하며(`PrivilegesRequired=admin`) 기본 경로는 `%ProgramFiles%\Music Motion Studio`입니다. 업데이트는 일반 사용자 권한으로 Setup을 시작하고 Setup 자체가 권한 상승을 요청합니다. `/AUTOMATICUPDATE=1 /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /UPDATEPID=<종료할 앱 PID> /UPDATERECOVERY=<복구본 경로> /UPDATEREADY=<준비 완료 신호 파일>`을 전달합니다. 설치된 앱은 `/DIR=<현재 실행 중인 앱 폴더>`를 전달해 사용자 지정 경로도 유지합니다. 편집기는 Setup의 준비 완료 신호를 받은 뒤 최신 작업을 다시 백업하고 종료합니다. 권한 요청을 취소하면 편집기는 열린 상태로 남습니다. Setup은 해당 프로세스가 종료된 뒤 기존 파일을 제자리에서 갱신하며, 성공했을 때만 원래 사용자의 일반 권한으로 `--resume-update <복구본 경로>`를 실행합니다. 이전 설치본의 제거 프로그램은 호출하지 않으며, 제거 시 설치 폴더 안의 사용자 파일은 삭제하지 않습니다.

설치 마법사는 루트의 `LICENSE.txt`를 Playlist Canvas Source-Available Noncommercial Share-Alike License 1.0 동의 화면으로 표시하며, 설치된 프로그램 폴더에도 같은 원문을 복사합니다.

완성된 설치 파일은 다음 위치에 생성됩니다.

```text
output-setup\Music Motion Studio-1.4.0.0-setup.exe
```

이 Setup EXE를 GitHub Release에 첨부합니다. 사용자는 ZIP을 직접 관리하지 않고 설치 마법사를 통해 프로그램 위치, 시작 메뉴와 바탕 화면 바로가기를 설정할 수 있습니다.

앱의 자동 업데이트가 Setup 자산을 찾고 검증할 수 있도록 릴리즈를 정식 공개 상태로 만들고 다음 조건을 지킵니다.

- 태그는 `1.4.0.0`처럼 네 자리 버전 형식을 사용합니다. (`v` 접두사도 허용됩니다.)
- 새 Setup 자산 이름에는 `Music Motion Studio`와 `setup`이 포함되어야 하며 확장자는 `.exe`여야 합니다. 최신 앱은 `tharu8813/Music-Motion-Studio`의 릴리즈만 조회하고, 다운로드 URL도 해당 저장소인지 검증합니다. 기존 `Playlist-Canvas` 앱의 업데이트 경로는 이 패키징 변경으로 수정되지 않으며, 구버전 릴리즈 운영은 별도로 관리합니다.
- Setup 파일은 반드시 이 저장소의 GitHub Release 자산으로 직접 첨부합니다. 외부 다운로드 URL은 앱이 거부합니다.
- 초안 또는 사전 릴리즈는 자동 업데이트 대상에서 제외됩니다.
- 릴리즈 본문은 프로그램의 업데이트 알림창에 그대로 표시되므로 사용자용 변경 내용을 작성합니다.

## 4. 배포 전 확인

새 Windows 사용자 계정 또는 가상 머신에서 다음을 확인합니다.

1. `Music Motion Studio-1.4.0.0-setup.exe`로 설치와 제거가 정상 동작하는지 확인합니다.
2. 설치된 `Music Motion Studio.exe`가 실행되는지 확인합니다.
3. 설정 화면에서 언어·테마·출력 폴더를 저장하고 재시작 후 유지되는지 확인합니다.
4. 설정 화면의 FFmpeg 다운로드를 사용해 설치와 SHA-256 검증이 완료되는지 확인합니다.
5. 정적 Canvas 프로젝트를 MP4로 내보내고 대량의 PNG 대신 `canvas-base.mkv` 스트림이 준비되는지 로그로 확인합니다.
6. 비주얼라이저 또는 파티클의 앞뒤에 정적 레이어를 둔 프로젝트를 내보내 레이어 순서와 반투명 가장자리가 미리보기와 일치하는지 확인합니다.
7. 내보내기를 준비 중과 최종 인코딩 중 각각 취소하고 임시 파일이 정리되는지 확인합니다.
8. `%LOCALAPPDATA%\PlaylistCanvas\logs\playlist-canvas.log`에 오류 로그가 생성되는지 확인합니다.

실제 FFmpeg 스트리밍 경계는 개발 환경에서 다음 선택형 종단 테스트로도 확인할 수 있습니다.
테스트는 짧은 무음 오디오, 불투명 기본 화면, 비주얼라이저와 반투명 Z 레이어를 최종 MP4로
인코딩한 뒤 첫 프레임의 합성 픽셀을 검사합니다.

```powershell
$env:PLAYLIST_CANVAS_TEST_FFMPEG = "C:\path\to\ffmpeg.exe"
python -m unittest tests.test_ffmpeg_streaming_integration -v
```

## 5. 배포 시 유의 사항

- 앱이 내려받는 FFmpeg Windows 빌드는 GPL 라이선스입니다. 배포 페이지와 앱 안내에서 FFmpeg 및 해당 배포본의 라이선스 정보를 함께 고지합니다.
- Music Motion Studio 원본이나 수정본은 비상업적으로만 무료 배포할 수 있으며, `LICENSE.txt`, 저작권 표시, 원본 저장소 주소와 수정 사실을 유지해야 합니다.
- 수정본 배포 시 동일한 라이선스를 적용하고 완전한 대응 소스를 실행 파일과 동시에 무료로 공개해야 합니다.
- 프로그램으로 만든 영상이나 콘텐츠의 판매·수익화·공개 업로드에는 제한이 없습니다. 온라인 설명에 Music Motion Studio 사용 사실과 원본 저장소 주소를 표시하는 것은 권장 사항이며 의무가 아닙니다.
- Music Motion Studio 자체와 수정본의 상업적 수정·판매·유료 배포·유료 접근 제공은 금지됩니다.
- FFmpeg를 별도로 번들하기로 정책을 변경할 경우 `ffmpeg.exe`와 필요한 라이선스 파일만 선별하고, 배포본 크기와 해당 바이너리의 출처를 다시 검토합니다.
- 코드 서명을 적용하면 Windows SmartScreen 경고를 줄일 수 있습니다. 서명 인증서와 타임스탬프 서버는 배포 환경에서 관리합니다.
- FFmpeg 설치 폴더와 로그는 `%LOCALAPPDATA%\PlaylistCanvas` 아래에 있으므로 관리자 권한 없이 동작합니다. 이전 버전에서 저장한 설정과 복구본은 첫 실행 시 자동으로 이전됩니다.
