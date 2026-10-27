# PressPatrol · 프레스 패트롤

**인터넷신문의 규정 준수 여부를 점검하는 디지털 검사관.**

**「신문 등의 진흥에 관한 법률」 및 같은 법 시행령의 준수 여부 점검을 돕는 도구입니다.** 현재 홈페이지 운영현황과 제21조 필요적 게재사항을 중심으로 수집·AI 검토·증빙 보존을 수행하며, **최종 판단은 사람이 증빙을 다시 확인한 뒤 내려야 합니다.**

![손그림 파이프라인](assets/pipeline.svg)

- **규칙 우선:** 필수 8항목과 교차 인정 규칙을 구현하고 최초 규칙 결과를 동결합니다.
- **근거 보존:** 렌더 HTML·스크린샷을 저장하고 원문 근거·위치·판단 이력을 연결합니다.
- **단계별 AI:** 텍스트로 먼저 검토하고 필요한 건만 비전·날짜·검색 판단으로 보냅니다.
- **사람 검수:** 권한·업무 배정·단계 승인·동시 수정 충돌·변경 이력을 관리합니다.

## 기능별 안내

- [파이프라인 실행](ver_1/skill/README.md) / [API 키 설정](ver_1/skill/SETUP.md)
- [판정기준과 원래 규칙](ver_1/skill/references/README.md) / [규칙 엔진 구현](ver_1/skill/scripts/md_core/README.md)
- [에이전트 역할·입출력·실제 소스](ver_1/skill/agents/README.md)
- [단가·캐시·비용 계산과 한계](ver_1/skill/COSTS.md)
- [관리자·검토자 UI](ver_1/review_ui/README.md)
- [가상 입력 20건 샘플](examples/registry/README.md)
- [설계 결정](docs/design-decisions.md) / [검증 범위](docs/validation.md)

실행 명령은 [영문 README](README.md)에 있습니다. 입력 형식만 보존한 가상 데이터 20건을 포함하며 실제 조사 데이터·연락처·계정 DB·원본 증빙·API 키는 올리지 않습니다.
