Feature: Response co bang chung va ha tang mem
  Bao criteria AC-S1 den AC-S7. Chua chay (cho crawl full).

  Scenario: Ten nguoi phai kem bang chung
    Given ban nhap muon neu "Nguyen Van A"
    And doan trich co ten do
    When sinh cau tra loi
    Then ten xuat hien kem nguon trich dan

  Scenario: Ten khong bang chung thi cat bo
    Given ban nhap muon neu "Tran Thi B"
    And khong doan trich nao chua ten do
    When sinh cau tra loi
    Then ten do bi cat
    And neu het ten thi noi khong co du lieu

  Scenario: Thieu bang chung thi noi khong biet
    Given tap retrieval rong
    When sinh cau tra loi
    Then co "khong co du lieu"
    And co sufficient false

  Scenario: Tang tra loi van chan du retrieval co gi
    Given retrieval tra ve anh tre em
    When hoi "ai trong anh nay"
    Then tu choi du co ket qua retrieval

  Scenario: LLM hong van tra nguon
    Given LLM mat ket noi
    When hoi bat ky
    Then phan dap an sinh de trong
    And danh sach nguon van day du
    And request khong sap

  Scenario: LLM doc ca doan chu khong chi doan trich
    Given doan tim duoc dai 1400 ky tu va dap an nam o cuoi doan
    When sinh cau tra loi
    Then ngu canh gui LLM chua ca doan
    And giao dien van chi hien doan trich ngan

  Scenario: Qua han thi tra nguon kem ghi chu
    Given LLM tra loi cham hon tran thoi gian
    When hoi bat ky
    Then request ket thuc trong tran thoi gian
    And van co danh sach nguon kem ghi chu qua han
