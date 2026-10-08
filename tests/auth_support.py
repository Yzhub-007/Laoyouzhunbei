from auth import AuthService

TEST_PASSWORD = 'Local-test-passphrase-2026!'


def admin_service(runtime):
    service = AuthService(runtime)
    issued = service.setup({'setup_code': service.setup_path.read_text(),
                            'username': 'test-admin', 'password': TEST_PASSWORD})
    return service, issued


def session_headers(http, issued):
    return {'Cookie': f"campus_session_{http.server_port}={issued['session_id']}",
            'Origin': f'http://127.0.0.1:{http.server_port}', 'X-App-Token': issued['csrf_token']}
