// Native boundary: secrets travel through stdin/stdout pipes, never argv or files.
import Foundation
import LocalAuthentication
import Security

let noInteraction = LAContext()
noInteraction.interactionNotAllowed = true

let service = "com.softrobots.work-organizer.sessions.v1"

enum Failure: Error { case unavailable }

func key(create: Bool, mode: String) throws -> SecKey {
  let hardware = mode == "secure-enclave"
  let tag = Data((hardware
    ? "com.softrobots.work-organizer.device-key.v1"
    : "com.softrobots.work-organizer.software-device-key.v1").utf8)
  let query: [String: Any] = [
    kSecClass as String: kSecClassKey,
    kSecAttrApplicationTag as String: tag,
    kSecAttrKeyType as String: kSecAttrKeyTypeECSECPrimeRandom,
    kSecReturnRef as String: true, kSecUseAuthenticationContext as String: noInteraction,
  ]
  var result: CFTypeRef?
  let status = SecItemCopyMatching(query as CFDictionary, &result)
  if status == errSecSuccess {
    let existing = result as! SecKey
    guard let attributes = SecKeyCopyAttributes(existing) as? [String: Any] else {
      throw Failure.unavailable
    }
    let isHardware = attributes[kSecAttrTokenID as String] as? String == kSecAttrTokenIDSecureEnclave as String
    guard isHardware == hardware else { throw Failure.unavailable }
    return existing
  }
  guard status == errSecItemNotFound && create else { throw Failure.unavailable }
  var error: Unmanaged<CFError>?
  var privateAttributes: [String: Any] = [
    kSecAttrIsPermanent as String: true,
    kSecAttrApplicationTag as String: tag,
  ]
  var attributes: [String: Any] = [
    kSecAttrKeyType as String: kSecAttrKeyTypeECSECPrimeRandom,
    kSecAttrKeySizeInBits as String: 256,
  ]
  if hardware {
    guard let control = SecAccessControlCreateWithFlags(
      nil, kSecAttrAccessibleWhenUnlockedThisDeviceOnly, .privateKeyUsage, &error)
    else { throw Failure.unavailable }
    privateAttributes[kSecAttrAccessControl as String] = control
    attributes[kSecAttrTokenID as String] = kSecAttrTokenIDSecureEnclave
  } else {
    // Explicit opt-in: traditional login Keychain, without hardware protection.
    // Do not use Secure Enclave access-control attributes in this backend.
    attributes[kSecUseDataProtectionKeychain as String] = false
    privateAttributes[kSecAttrIsExtractable as String] = false
  }
  attributes[kSecPrivateKeyAttrs as String] = privateAttributes
  guard let generated = SecKeyCreateRandomKey(attributes as CFDictionary, &error) else {
    throw Failure.unavailable
  }
  return generated
}

func publicDER(_ privateKey: SecKey) throws -> Data {
  var error: Unmanaged<CFError>?
  guard let publicKey = SecKeyCopyPublicKey(privateKey),
    let raw = SecKeyCopyExternalRepresentation(publicKey, &error) as Data?,
    raw.count == 65, raw.first == 4
  else { throw Failure.unavailable }
  // RFC 5480 SubjectPublicKeyInfo for id-ecPublicKey / prime256v1, followed by X9.63 point.
  return Data([
    0x30, 0x59, 0x30, 0x13, 0x06, 0x07, 0x2a, 0x86, 0x48, 0xce, 0x3d, 0x02, 0x01,
    0x06, 0x08, 0x2a, 0x86, 0x48, 0xce, 0x3d, 0x03, 0x01, 0x07, 0x03, 0x42, 0x00,
  ]) + raw
}

func execute(_ input: [String: Any]) throws -> [String: Any] {
  guard let operation = input["operation"] as? String else { throw Failure.unavailable }
  let mode = input["key_mode"] as? String ?? "secure-enclave"
  guard mode == "secure-enclave" || mode == "standard-keychain" else {
    throw Failure.unavailable
  }
  if operation == "enroll" || operation == "sign" {
    let privateKey = try key(create: operation == "enroll", mode: mode)
    var output: [String: Any] = ["public_key": try publicDER(privateKey).base64EncodedString()]
    if operation == "sign" {
      guard let encoded = input["message"] as? String, let message = Data(base64Encoded: encoded),
        message.count <= 4096
      else { throw Failure.unavailable }
      var error: Unmanaged<CFError>?
      guard
        let signature = SecKeyCreateSignature(
          privateKey, .ecdsaSignatureMessageX962SHA256,
          message as CFData, &error) as Data?
      else { throw Failure.unavailable }
      output["signature"] = signature.base64EncodedString()
    }
    return output
  }
  guard let account = input["account"] as? String, account.count == 64 else {
    throw Failure.unavailable
  }
  let query: [String: Any] = [
    kSecClass as String: kSecClassGenericPassword,
    kSecAttrService as String: service, kSecAttrAccount as String: account,
    kSecAttrSynchronizable as String: false,
    kSecUseAuthenticationContext as String: noInteraction,
  ]
  if operation == "load" {
    var read = query
    read[kSecReturnData as String] = true
    var result: CFTypeRef?
    let status = SecItemCopyMatching(read as CFDictionary, &result)
    if status == errSecItemNotFound { return ["value": NSNull()] }
    guard status == errSecSuccess, let data = result as? Data else { throw Failure.unavailable }
    return ["value": try JSONSerialization.jsonObject(with: data)]
  }
  if operation == "delete" {
    let status = SecItemDelete(query as CFDictionary)
    guard status == errSecSuccess || status == errSecItemNotFound else { throw Failure.unavailable }
    return [:]
  }
  if operation == "save", let value = input["value"] as? [String: Any] {
    let data = try JSONSerialization.data(withJSONObject: value)
    guard data.count <= 32768 else { throw Failure.unavailable }
    let updates: [String: Any] = [
      kSecValueData as String: data,
      kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
    ]
    var status = SecItemUpdate(query as CFDictionary, updates as CFDictionary)
    if status == errSecItemNotFound {
      var add = query
      add.removeValue(forKey: kSecUseAuthenticationContext as String)
      updates.forEach { add[$0.key] = $0.value }
      status = SecItemAdd(add as CFDictionary, nil)
    }
    guard status == errSecSuccess else { throw Failure.unavailable }
    return [:]
  }
  throw Failure.unavailable
}

do {
  let raw = FileHandle.standardInput.readDataToEndOfFile()
  guard raw.count <= 65536,
    let input = try JSONSerialization.jsonObject(with: raw) as? [String: Any]
  else { throw Failure.unavailable }
  let data = try JSONSerialization.data(withJSONObject: execute(input))
  FileHandle.standardOutput.write(data)
} catch {
  // Never print OS errors, key material, tokens, input or chained details.
  FileHandle.standardError.write(Data("Device security operation unavailable\n".utf8))
  exit(1)
}
