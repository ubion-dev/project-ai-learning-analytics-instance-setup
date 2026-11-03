# 프론트 배포 갱신 계약

자동 갱신은 각 화면의 `frontend.enabled=true`와 명시한 `manifest_url`이 있을 때만
동작한다. 기본값은 꺼짐이다. 일반 외부 웹사이트의 HTML, ETag 또는 제목으로
새 배포를 추정하지 않는다. 실제 프론트 저장소에 이 계약을 연결하는 작업은 별도다.

## 배포 manifest

```json
{
  "schema_version": 1,
  "deployment_seq": 42,
  "build_id": "example-build-42",
  "ready": true
}
```

- HTTP 200과 `Cache-Control: no-store` 응답을 제공한다. 16KiB 이하의 JSON만 허용한다.
- `deployment_seq`는 양의 정수이며 배포마다 증가한다. rollback도 더 큰 순번이다.
- 같은 순번의 `build_id`를 변경하지 않는다. build ID는 영문·숫자로 시작하는
  최대 128자의 영문·숫자·마침표·밑줄·하이픈이다.
- 모든 정적 파일과 필요한 API가 준비된 뒤 `ready=true`를 게시한다.
- 주소에 계정·암호·인증 토큰을 넣지 않는다. TLS 검증은 유지하며 다른 출처로
  redirect하지 않는다. manifest에 화면 URL이나 실행할 JavaScript를 넣지 않는다.

## 실제 페이지 신호

```html
<meta name="ala-build-id" content="example-build-42">
<meta name="ala-app-ready" content="false">
```

`ala-build-id`는 해당 HTML/JavaScript가 실제 실행하는 빌드다. 앱 초기화와 표시할
데이터 준비가 끝났을 때만 다음과 같이 준비 신호를 바꾼다.

```javascript
await initializeApplication();
await loadRequiredDisplayData();
document.querySelector('meta[name="ala-app-ready"]').content = 'true';
```

HTTP 200이나 `load` 이벤트만으로 앱 준비를 대신하지 않는다. 앱 준비를 나중에
잃었을 때도 신호를 다시 `false`로 변경한다. 이 신호는 내부 데이터의 완전성이나
실제 모니터 표시를 ala가 독립적으로 검증한다는 의미가 아니다.

## 처리 순서와 장애

로컬 systemd 타이머가 60초 간격에 최대 10초의 분산 지연을 더해 확인한다.
동일한 새 배포가 연속 두 번 준비 상태여야 적용한다. 조회 실패나 미준비는
현재 화면을 바꾸지 않는다. 이미 해당 빌드와 준비 신호가 관측된 창은 이동하지 않는다.

실행 중인 대상만 순차 처리한다. runtime 예약과 구성 revision을 확인하고 대상의
지정 진입 URL로 한 번 이동한다. 새 문서 응답·load·빌드 ID·앱 준비 신호까지
확인한 뒤 다음 대상을 처리한다. 중지된 대상을 켜거나 같은 배포를 주기적으로
새로고침하지 않는다. 코드 전환 작업과 겹치면 완료 후 다시 확인한다.

대상별 자동 시도는 배포당 한 번이다. 실패하거나 적용 중 전원이 끊기면 그 배포의
나머지 대상을 보류한다. 다시 진행하려면 명시적 재시도가 필요하다. 앞서 성공한
대상은 재시도하지 않는다. 배포 이력에는 순번·빌드·대상·결과만 저장하고 manifest
주소, 페이지 원문, 쿠키, 스크립트 스택은 저장하지 않는다.

브라우저 제어는 해당 사용자 서비스가 생성한 private CDP 연결만 사용한다.
요청은 동일 사용자 소유 0600 JSON에 nonce·현재 실행 nonce·대상·구성 revision을
묶고 만료 시간을 둔다. 요청 접수는 완료와 다르다. 현재 페이지 URL 원문은
이 제어 파일이나 상태 기록에 보관하지 않는다.

`examples/frontend/`는 계약을 확인하는 합성 예제다. 운영 프론트 연동과 실제
Rocky GNOME·재부팅·모니터 수용은 별도로 검증한다.
