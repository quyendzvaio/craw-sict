Feature: Trich noi dung trang sach
  Bao criteria AC-H1 den AC-H7. Corpus chi chua noi dung chinh cua bai.

  Scenario: Chi lay vung noi dung chinh
    Given trang bai viet co logo menu sidebar va footer
    When parse trang
    Then chu anh video chi lay tu vung noi dung chinh
    And khong co khoi ngay dang va luot xem

  Scenario: Video sidebar khong gan cho bai
    Given sidebar nhung video YouTube chung cho moi trang
    When parse bai bat ky
    Then danh sach video cua bai khong chua video do

  Scenario: Khong nhan ra vung noi dung thi canh bao
    Given site doi giao dien khong selector nao khop
    When parse trang
    Then co canh bao va bo dem trang roi vao truong hop nay tang

  Scenario: Trang loi ma 200 khong duoc index
    Given trang tra 200 nhung noi dung la loi ASP.NET
    When xu ly trang
    Then trang khong vao corpus va duoc tinh la loi

  Scenario: Trang tag khong vao corpus
    Given lien ket "/vn/tags?p=1&q=Lap trinh"
    When dua vao hang doi
    Then lien ket bi bo qua

  Scenario: Media trang tri khong index
    Given logo xuat hien o 2000 bai
    When danh gia tan suat media
    Then logo khong duoc index

  Scenario: Doan chu lap o nhieu bai khong index
    Given doan chu giong het xuat hien o 30 bai
    When danh gia tan suat chunk
    Then doan do khong duoc index
