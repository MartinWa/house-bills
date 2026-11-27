"""Baseline Content-Security-Policy for responses that do not set their own."""

from django.utils.deprecation import MiddlewareMixin

BASELINE_CSP = "default-src 'self'; frame-ancestors 'none'"


class ContentSecurityPolicyMiddleware(MiddlewareMixin):
    """Apply the baseline CSP; stricter view-level policies (e.g. PDF sandbox) win."""

    def process_response(self, request, response):
        if "Content-Security-Policy" not in response.headers:
            response["Content-Security-Policy"] = BASELINE_CSP
        return response
