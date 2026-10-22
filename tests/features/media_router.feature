Feature: Router thanh phan cau tra loi (2 tin hieu: y dinh + bang chung)
  Bao criteria AC-R8 den AC-R10, AC-R12. Khong phai cau hoi nao cung co anh lien quan.

  Scenario: Cau hoi thong tin khong kem anh
    Given hoi "hoc phi nganh cong nghe thong tin bao nhieu"
    When router xep loai
    Then muc media la none
    And khong chay nhanh media
    And ngu canh gui LLM khong co caption anh
    And giao dien khong hien khung anh

  Scenario: Hoi thang ve anh thi bat buoc tim anh
    Given hoi "cho xem anh le khai giang 2025"
    When router xep loai
    Then muc media la required
    And chi tra anh vuot nguong diem goc

  Scenario: Hoi ve su kien thi anh la tuy chon va gan voi bai
    Given hoi "hoi cho MIP Fair 2026 SICT tham gia the nao"
    And bai dung dau ket qua chu la bai MIP Fair co 10 anh
    When router xep loai va chan theo bang chung
    Then muc media la optional
    And chi tra toi da 3 anh thuoc chinh bai do

  Scenario: Khong anh nao dat nguong thi tra rong khong tra anh sai
    Given muc media la required
    And moi anh tim duoc deu duoi nguong
    When tong hop ket qua
    Then danh sach media rong
    And co ghi chu khong tim thay anh phu hop

  Scenario: Khong co LLM van xep loai bang tu khoa
    Given LLM mat ket noi
    When hoi "video gioi thieu truong"
    Then muc media la required theo luat tu khoa

  Scenario: Hoi tai bieu mau tra kem file dinh kem
    Given hoi "tai quy dinh chuan dau ra ngoai ngu"
    When router xep loai
    Then co co muon file
    And ket qua co link file dinh kem kem trang chua no
