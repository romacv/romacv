<?php include 'header.inc' ?>
<?php include 'data.inc' ?>
    <main>
        <div class="container">
            <section class="projects">
                <h2>All Projects</h2>
                <?php
                    foreach ($data as $year => $iData):
                ?>
                <div class="year-block">
                    <h3><?=$year?></h3>
                    <div class="row">
                        <?php
                            foreach ($iData as $name => $iiData):
                        ?>
                        <div class="col-md-4 col-sm-6 col-12">
                            <a href="../project/<?=$name?>" style="background-image:url('../img/works/<?=$iiData['img']?>.jpg')" data-name="<?=$iiData['title']?>"></a>
                        </div>
                        <?php endforeach; ?>
                    </div>
                </div>
                <?php endforeach; ?>
            </section>
            <hr>
        </div>
    </main>
<?php include 'footer.inc' ?>