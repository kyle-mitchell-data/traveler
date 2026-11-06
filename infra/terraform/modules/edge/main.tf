variable "tower_url_domain" { type = string }
variable "router_source" { type = string }
variable "writer_dir" { type = string }
variable "log_retention_days" { type = number }

# ---------------------------------------------------------------------------
# Static site: private bucket, read only by this distribution.
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "site" {
  bucket_prefix = "traveler-site-"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "site" {
  bucket                  = aws_s3_bucket.site.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "aws_iam_policy_document" "site" {
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.site.arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.site.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "site" {
  bucket = aws_s3_bucket.site.id
  policy = data.aws_iam_policy_document.site.json
}

# ---------------------------------------------------------------------------
# Routing: key-value store plus the viewer-request function that reads it.
# ---------------------------------------------------------------------------

resource "aws_cloudfront_key_value_store" "routing" {
  name    = "traveler-routing"
  comment = "Where the traveler lives. Written at each hop; read by traveler-router."
}

# Seed only. Wake, sleep and the traveler own this value afterwards.
resource "aws_cloudfrontkeyvaluestore_key" "status" {
  key_value_store_arn = aws_cloudfront_key_value_store.routing.arn
  key                 = "status"
  value               = "resting"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_cloudfront_function" "router" {
  name                         = "traveler-router"
  runtime                      = "cloudfront-js-2.0"
  comment                      = "Routes /api/traveler/* to the live traveler"
  publish                      = true
  code                         = file(var.router_source)
  key_value_store_associations = [aws_cloudfront_key_value_store.routing.arn]
}

# ---------------------------------------------------------------------------
# Route writer: signs KeyValueStore writes for the traveler (decision D16).
# ---------------------------------------------------------------------------

data "archive_file" "writer" {
  type        = "zip"
  source_dir  = var.writer_dir
  output_path = "${path.root}/.build/route-writer.zip"
  excludes    = [".deps-stamp"]
}

data "aws_iam_policy_document" "lambda_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "writer" {
  name               = "traveler-route-writer"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

data "aws_iam_policy_document" "writer" {
  statement {
    actions = [
      "cloudfront-keyvaluestore:DescribeKeyValueStore",
      "cloudfront-keyvaluestore:UpdateKeys",
    ]
    resources = [aws_cloudfront_key_value_store.routing.arn]
  }
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.writer.arn}:*"]
  }
}

resource "aws_iam_role_policy" "writer" {
  name   = "route-writer"
  role   = aws_iam_role.writer.id
  policy = data.aws_iam_policy_document.writer.json
}

resource "aws_cloudwatch_log_group" "writer" {
  name              = "/aws/lambda/traveler-route-writer"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "writer" {
  function_name    = "traveler-route-writer"
  role             = aws_iam_role.writer.arn
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "handler.handler"
  filename         = data.archive_file.writer.output_path
  source_code_hash = data.archive_file.writer.output_base64sha256
  memory_size      = 256
  timeout          = 15

  environment {
    variables = { KVS_ARN = aws_cloudfront_key_value_store.routing.arn }
  }

  logging_config {
    log_format = "JSON"
    log_group  = aws_cloudwatch_log_group.writer.name
  }
}

# ---------------------------------------------------------------------------
# Distribution
# ---------------------------------------------------------------------------

resource "aws_cloudfront_origin_access_control" "s3" {
  name                              = "traveler-site"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_origin_access_control" "lambda" {
  name                              = "traveler-lambda"
  origin_access_control_origin_type = "lambda"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

data "aws_cloudfront_cache_policy" "optimized" {
  name = "Managed-CachingOptimized"
}

data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}

# Lambda Function URLs reject a forwarded Host header.
data "aws_cloudfront_origin_request_policy" "all_but_host" {
  name = "Managed-AllViewerExceptHostHeader"
}

# Decision D6: every viewer shares one origin call per second per edge cache.
resource "aws_cloudfront_cache_policy" "tower" {
  name        = "traveler-tower-1s"
  min_ttl     = 0
  default_ttl = 1
  max_ttl     = 5

  parameters_in_cache_key_and_forwarded_to_origin {
    enable_accept_encoding_brotli = true
    enable_accept_encoding_gzip   = true
    cookies_config {
      cookie_behavior = "none"
    }
    headers_config {
      header_behavior = "none"
    }
    query_strings_config {
      query_string_behavior = "whitelist"
      query_strings {
        items = ["limit"]
      }
    }
  }
}

resource "aws_cloudfront_distribution" "site" {
  enabled             = true
  comment             = "traveler"
  default_root_object = "index.html"
  http_version        = "http2and3"
  is_ipv6_enabled     = true
  price_class         = "PriceClass_All"

  origin {
    origin_id                = "site"
    domain_name              = aws_s3_bucket.site.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.s3.id
  }

  origin {
    origin_id                = "tower"
    domain_name              = var.tower_url_domain
    origin_access_control_id = aws_cloudfront_origin_access_control.lambda.id
    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  # Placeholder: traveler-router replaces this origin on every request.
  origin {
    origin_id                = "traveler"
    domain_name              = var.tower_url_domain
    origin_access_control_id = aws_cloudfront_origin_access_control.lambda.id
    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    target_origin_id       = "site"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    cache_policy_id        = data.aws_cloudfront_cache_policy.optimized.id
    compress               = true
  }

  ordered_cache_behavior {
    path_pattern             = "/api/tower/*"
    target_origin_id         = "tower"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = aws_cloudfront_cache_policy.tower.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_but_host.id
    compress                 = true
  }

  ordered_cache_behavior {
    path_pattern             = "/api/traveler/*"
    target_origin_id         = "traveler"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_but_host.id
    compress                 = true

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.router.arn
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  # Decision D10: default *.cloudfront.net certificate until a domain exists.
  viewer_certificate {
    cloudfront_default_certificate = true
  }
}

output "distribution_arn" { value = aws_cloudfront_distribution.site.arn }
output "domain" { value = aws_cloudfront_distribution.site.domain_name }
output "site_bucket" { value = aws_s3_bucket.site.id }
output "kvs_arn" { value = aws_cloudfront_key_value_store.routing.arn }
output "writer_name" { value = aws_lambda_function.writer.function_name }
output "writer_arn" { value = aws_lambda_function.writer.arn }
