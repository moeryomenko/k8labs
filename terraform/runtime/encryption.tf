# ---------------------------------------------------------------------------
# encryption.tf — AES-256 encryption key + EncryptionConfiguration manifest.
#
# Mirrors ansible/roles/certs/tasks/main.yml:414-433: a random 32-char
# ASCII letters/digits secret (random_password), base64-encoded, published as
# key1 under the aescbc provider with identity fallback. Written 0644.
# ---------------------------------------------------------------------------

resource "random_password" "encryption_key" {
  length      = 32
  special     = false
  upper       = true
  lower       = true
  numeric     = true
  min_upper   = 1
  min_lower   = 1
  min_numeric = 1
}

resource "local_file" "encryption_config" {
  filename = "${local.pki_dir}/encryption-config.yaml"
  content = templatefile("${path.module}/templates/encryption-config.yaml.tftpl", {
    encryption_key_base64 = base64encode(random_password.encryption_key.result)
  })
  file_permission = "0644"
}
