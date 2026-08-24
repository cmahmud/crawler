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


def test_dynamic_placeholder_shell_requests_browser() -> None:
    assessment = assess_rendering(
        """
        <html><body>
          <nav><a href="/">Quotes to Scrape</a><a href="/login">Login</a></nav>
          <main><div id="quotesPlaceholder"></div></main>
          <footer><p>Quotes by GoodReads.com. Made with care by Zyte.</p></footer>
          <script src="/static/jquery.js"></script>
          <script src="/static/main.js"></script>
        </body></html>
        """
    )

    assert assessment.visible_text_length < 200
    assert assessment.requires_browser is True
    assert "dynamic-content-placeholder" in assessment.reasons


def test_inline_document_write_shell_requests_browser() -> None:
    assessment = assess_rendering(
        """
        <html><body>
          <nav><a href="/">Quotes to Scrape</a><a href="/login">Login</a></nav>
          <main></main>
          <footer><p>Quotes by GoodReads.com. Made with care by Zyte.</p></footer>
          <script>
            var data = [{"text": "A quote", "author": {"name": "Author"}}];
            for (var i in data) {
              document.write(
                '<div class="quote"><span class="text">'
                + data[i].text
                + '</span></div>'
              );
            }
          </script>
        </body></html>
        """
    )

    assert assessment.visible_text_length < 200
    assert assessment.requires_browser is True
    assert "inline-dom-generation" in assessment.reasons


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


def test_short_static_page_with_scripts_stays_on_http_without_dynamic_container() -> None:
    assessment = assess_rendering(
        """
        <html><body><main>
          <h1>About</h1>
          <p>Short but complete server-rendered page.</p>
        </main>
        <script src="/analytics.js"></script>
        <script src="/menu.js"></script>
        </body></html>
        """
    )

    assert assessment.requires_browser is False
    assert "dynamic-content-placeholder" not in assessment.reasons
    assert "inline-dom-generation" not in assessment.reasons


def test_short_static_page_with_inline_ui_script_stays_on_http() -> None:
    assessment = assess_rendering(
        """
        <html><body><main>
          <h1>Contact</h1>
          <p>Email us for assistance.</p>
        </main>
        <script>
          const button = document.querySelector('#menu');
          if (button) button.addEventListener('click', () => console.log('open'));
        </script>
        </body></html>
        """
    )

    assert assessment.requires_browser is False
    assert "inline-dom-generation" not in assessment.reasons


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
