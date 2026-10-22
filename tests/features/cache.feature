Feature: Cache da tang
  Bao criteria AC-K1 den AC-K5. Chua chay (cho crawl full).

  Scenario: Anh duoc edge cache 30 ngay
    Given GET "/media/abc123.png"
    When qua reverse proxy 2 lan
    Then lan 2 duoc phuc vu tu cache
    And khong cham application server

  Scenario: Giao dien cache ngan
    Given GET trang chu giao dien
    When qua reverse proxy
    Then TTL khong qua 10 phut

  Scenario: API POST khong cache
    Given POST "/query" cung cau hoi 2 lan
    When qua reverse proxy
    Then ca 2 lan deu toi application server

  Scenario: Hoi lap lai dung L2 khong tinh lai
    Given cau hoi va top_k giong het lan truoc
    When hoi lai
    Then khong chay lai embedding va retrieval
    And ket qua giong lan truoc

  Scenario: Bai sua thi vo hieu cache lien quan
    Given bai 1 doi noi dung
    When co su kien sua bai
    Then cache khoa theo URL bai 1 bi xoa
    And anh bat bien theo key van giu

  Scenario: Cache hong thi di thang backend
    Given edge cache day hoac hong
    When co request
    Then request van toi backend
    And he thong khong sap

  Scenario: Stream chat khong bi don cuc
    Given POST "/chat" tra stream
    When qua reverse proxy
    Then tung doan du lieu toi ngay khong doi du
