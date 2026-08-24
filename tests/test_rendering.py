from syndcrawler.core.rendering import assess_rendering


def test_empty_client_shell_requests_browser() -> None:
    assessment = assess_rendering(
        """
        <html><body>
          <div id="root"></div>
          <noscript>Please enable JavaScript to continue.</noscript>
          <script src="/runtime.js"></script>
          <script src="/vendor.js"></script>
          <script src="/app.js"></script>
        </body></html>
        """
    )

    assert assessment.requires_browser is True
    assert "client-app-root" in assessment.reasons
    assert "javascript-required-message" in assessment.reasons


def test_static_document_stays_on_http() -> None:
    assessment = assess_rendering(
        """
        <html><body><main>
          <h1>Documentation</h1>
          <p>This page contains useful server-rendered content for a crawler.</p>
          <p>There is no client application shell and no reason to launch a browser.</p>
        </main></body></html>
        """
    )

    assert assessment.requires_browser is False


def test_ssr_framework_page_does_not_escalate_just_for_app_root() -> None:
    paragraph = "Server rendered product information with useful details. " * 8
    assessment = assess_rendering(
        f"""
        <html><body>
          <div id="__next"><main><h1>Product</h1><p>{paragraph}</p></main></div>
          <script src="/_next/runtime.js"></script>
          <script src="/_next/app.js"></script>
          <script src="/_next/chunk.js"></script>
        </body></html>
        """
    )

    assert assessment.visible_text_length >= 300
    assert assessment.requires_browser is False
    assert "meaningful-server-rendered-content" in assessment.reasons
