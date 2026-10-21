Feature: Chong anh video trung
  Bao criteria AC-B1 den AC-B6: 1 media nhieu noi xuat hien, xu ly dung 1 lan.

  Scenario: Cung anh khac kich thuoc URL duoc nhan ra 1 anh
    Given anh goc tai "https://vd/x.jpg" va anh resize tai "https://vd/x.jpg?w=800"
    When chay so trung
    Then chi co 1 ban ghi media
    And ca 2 URL cung tro ve ban ghi do

  Scenario: Anh resize nen lai khong tai URL goc van trung
    Given 2 file khac URL nhung SHA-256 giong nhau
    When chay so trung
    Then chi xu ly OCR va embedding 1 lan

  Scenario: Anh crop nhe van trung o tang embedding
    Given 2 anh SHA khac nhau, khoang cach pHash nho, embedding gan nhau
    When chay so trung
    Then gop thanh 1 media

  Scenario: Anh chup lien tiep cung su kien khong duoc gop xoa
    Given 2 anh lien tiep giong nhau 95% nhung la 2 khoanh khac khac nhau
    When chay so trung
    Then giu 2 media rieng trong cung nhom gan-giong

  Scenario: Cung anh dung lai o nhieu bai
    Given anh A xuat hien o bai 1 caption "khai mac" va bai 2 caption "be mac"
    When nap ca 2 bai
    Then co 1 media va 2 ban ghi xuat hien
    And dinh danh cuoi hop nhat ca 2 caption

  Scenario: Xoa 1 bai khong xoa media con dung o bai khac
    Given media co 2 xuat hien, xoa bai 1
    When dem tham chieu
    Then media van ton tai voi 1 xuat hien
    When xoa not bai cuoi
    Then media moi duoc xoa

  Scenario: Video YouTube lay ma video lam khoa
    Given 2 bai nhung cung video YouTube "abc123"
    When chay so trung
    Then chi lay keyframe 1 lan
